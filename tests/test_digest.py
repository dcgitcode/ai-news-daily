import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock

from news_digest.core import Article, canonical_url, clean, load_state, mark, matches, pending, rank, render, save_state
from news_digest.delivery import send, wecom_markdown
from news_digest.sources import collect, extract_image, fetch_feed
from news_digest.__main__ import main


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.config = json.loads(Path('config.json').read_text(encoding='utf-8'))
        self.config['chinese_only'] = False

    def article(self, **kwargs):
        values = dict(title='AI 机器人模型更新', url='https://example.com/a',
                      summary='机器人学习能力得到改进', published=self.now, source='test', weight=4)
        values.update(kwargs)
        return Article(**values)

    def test_url_normalization_and_unsafe_url(self):
        self.assertEqual(canonical_url('https://example.com/a/?utm_source=x&b=1#x'), 'https://example.com/a?b=1')
        self.assertEqual(canonical_url('http://arxiv.org/abs/2401.00001v2'), 'https://arxiv.org/abs/2401.00001')
        with self.assertRaises(ValueError):
            canonical_url('javascript:alert(1)')

    def test_keyword_boundary(self):
        self.assertFalse(matches('AI', 'paid railway'))
        self.assertTrue(matches('AI', 'AI-based model'))
        self.assertTrue(matches('具身智能', '学习具身智能技术'))

    def test_date_filter_and_deduplication(self):
        items = [self.article(), self.article(url='https://example.com/a?utm_source=x'),
                 self.article(title='Old AI', published=self.now - timedelta(days=5)),
                 self.article(title='Future AI', published=self.now + timedelta(days=1)),
                 self.article(title='Undated AI', published=None),
                 self.article(title='Casino AI', url='https://example.com/b')]
        self.assertEqual(len(rank(items, self.config, self.now)), 1)

    def test_no_keyword_is_excluded(self):
        self.assertEqual(rank([self.article(title='Weather', summary='sunny')], self.config, self.now), [])

    def test_chinese_only_checks_title_and_summary(self):
        self.config['chinese_only'] = True
        chinese = self.article()
        english = self.article(title='AI robotics release', summary='Robot learning update', url='https://example.com/en')
        mixed = self.article(summary='This is an English description of the AI model.', url='https://example.com/mixed')
        self.assertEqual(rank([chinese, english, mixed], self.config, self.now), [chinese])

    def test_safe_html(self):
        page, _ = render([self.article(title='<img onerror=bad>', summary='<script>bad</script>')], 'test')
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img onerror', page)
        self.assertEqual(clean('<p>Hello</p><script>bad</script>'), 'Hello')

    def test_channel_state_and_atomic_roundtrip(self):
        item = self.article()
        state = {'version': 1, 'channels': {}}
        mark(state, 'email', [item], self.now)
        self.assertEqual(pending([item], state, 'email', 12), [])
        self.assertEqual(len(pending([item], state, 'pushplus', 12)), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            save_state(path, state)
            self.assertEqual(load_state(path), state)
            path.write_text('invalid', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_state(path)

    def test_rss_parse_and_undated(self):
        client = Mock()
        client.get.return_value.content = b'''<rss version="2.0"><channel><title>Feed</title>
        <item><title>AI test</title><link>https://example.com/a</link>
        <pubDate>Sun, 06 Sep 2026 00:00:00 GMT</pubDate><description>Hello</description></item>
        <item><title>AI undated</title><link>https://example.com/b</link></item></channel></rss>'''
        items = fetch_feed(client, 'https://example.com/feed', 'test', 2)
        self.assertEqual(items[0].published.year, 2026)
        self.assertIsNone(items[1].published)

    @patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'test'})
    @patch('news_digest.delivery.requests.post')
    def test_pushplus_checks_application_result(self, post):
        post.return_value.json.return_value = {'code': 500}
        with self.assertRaises(RuntimeError):
            send('pushplus', 'test', 'html', 'text')
        post.return_value.json.return_value = {'code': 200, 'data': 'receipt'}
        self.assertEqual(send('pushplus', 'test', 'html', 'text'), 'pushplus_queued:receipt')

    @patch('news_digest.sources.fetch_feed', side_effect=RuntimeError('source unavailable'))
    def test_all_sources_failed_is_error(self, fetch):
        config = copy.deepcopy(self.config)
        config['github']['enabled'] = False
        with self.assertRaises(RuntimeError):
            collect(config, self.now)

    @patch('news_digest.sources.fetch_feed')
    def test_partial_source_failure_keeps_available_news(self, fetch):
        config = copy.deepcopy(self.config)
        config['github']['enabled'] = False
        config['arxiv']['enabled'] = False
        fetch.side_effect = [RuntimeError('unavailable'), [self.article()], []]
        articles, status = collect(config, self.now)
        self.assertEqual(len(articles), 1)
        self.assertFalse(status[0]['ok'])
        self.assertTrue(status[1]['ok'])

    def test_partial_failure_retry_and_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / 'state.json')
            env = {'CHANNELS': 'email,pushplus', 'STATE_PATH': state_path, 'REPORT_DIR': directory,
                   'PUSHPLUS_TOKEN': 'test-token', 'SMTP_USER': 'u', 'SMTP_PASSWORD': 'p',
                   'EMAIL_TO': 'to@example.com'}
            with patch.dict(os.environ, env), patch('sys.argv', ['digest']), \
                 patch('news_digest.__main__.validate_channels'), \
                 patch('news_digest.__main__.collect', return_value=([self.article()], [{'ok': True}])), \
                 patch('news_digest.__main__.send', side_effect=['smtp_accepted', RuntimeError('failed')]) as delivery:
                self.assertEqual(main(), 1)
                self.assertEqual(delivery.call_count, 2)
            with patch.dict(os.environ, env), patch('sys.argv', ['digest']), \
                 patch('news_digest.__main__.validate_channels'), \
                 patch('news_digest.__main__.collect', return_value=([self.article()], [{'ok': True}])), \
                 patch('news_digest.__main__.send', return_value='queued') as delivery:
                self.assertEqual(main(), 0)
                self.assertEqual(delivery.call_args.args[0], 'pushplus')
                self.assertEqual(delivery.call_count, 1)
            before = Path(state_path).read_bytes()
            with patch.dict(os.environ, env), patch('sys.argv', ['digest', '--demo']), \
                 patch('news_digest.__main__.send') as delivery:
                self.assertEqual(main(), 0)
                delivery.assert_not_called()
            self.assertEqual(Path(state_path).read_bytes(), before)


    def test_unconfigured_channel_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'CHANNELS': 'email,pushplus,wecombot', 'STATE_PATH': str(Path(directory) / 's.json'),
                   'REPORT_DIR': directory, 'PUSHPLUS_TOKEN': '', 'WECOM_WEBHOOK': '',
                   'SMTP_USER': '', 'SMTP_PASSWORD': '', 'EMAIL_TO': ''}
            # 三个渠道全部缺凭证：send 应一次都不被调用且不抛错。
            with patch.dict(os.environ, env), patch('sys.argv', ['digest']), \
                 patch('news_digest.__main__.validate_channels'), \
                 patch('news_digest.__main__.load_dotenv'), \
                 patch('news_digest.__main__.collect', return_value=([self.article()], [{'ok': True}])), \
                 patch('news_digest.__main__.send') as delivery:
                self.assertEqual(main(), 0)
                delivery.assert_not_called()

    def test_wecombot_rejects_nonzero_errcode(self):
        with patch.dict(os.environ, {'WECOM_WEBHOOK': 'https://example.invalid/hook'}), \
             patch('news_digest.delivery.requests.post') as post:
            post.return_value.json.return_value = {'errcode': 93000}
            with self.assertRaises(RuntimeError):
                send('wecombot', 't', 'html', '1. 标题\nmeta\n摘要\nhttps://example.com')

    def test_wecombot_markdown_stays_within_limit(self):
        articles = [self.article(title=f'超长标题测试第{i}条关于大模型与具身智能的进展',
                                 summary='这是一段足够长的中文摘要' * 20) for i in range(1, 13)]
        _, plain = render(articles, 'AI 每日新闻 测试')
        content = wecom_markdown('AI 每日新闻 测试', plain)
        self.assertLessEqual(len(content.encode('utf-8')), 4096)
        self.assertIn('[超长标题测试第1条', content)

    def test_extract_image_prefers_media_and_validates(self):
        media_entry = {'media_content': [{'url': 'https://cdn.example.com/pic'}],
                       'summary': '<img src="https://x.example.com/a.jpg">'}
        self.assertEqual(extract_image(media_entry), 'https://cdn.example.com/pic')
        body_entry = {'summary': '<p><img src="https://x.example.com/a.png"></p>'}
        self.assertEqual(extract_image(body_entry), 'https://x.example.com/a.png')
        bad_entry = {'summary': '<img src="javascript:alert(1)"><img src="/relative.png">'}
        self.assertEqual(extract_image(bad_entry), '')
        self.assertEqual(extract_image({}), '')


if __name__ == '__main__':
    unittest.main()

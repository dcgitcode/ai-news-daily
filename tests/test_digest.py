import copy
import json
import os
import requests
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock

from news_digest.core import Article, canonical_url, clean, load_state, mark, matches, pending, rank, render, save_state
from news_digest.delivery import (send, wecom_markdown, wecom_app_cards, pushplus_html,
                                  serverchan_markdown, serverchan_url, telegram_html)
from news_digest.sources import collect, extract_image, fetch_feed
from news_digest.extras import (collect_extras, fetch_arxiv, fetch_chinese, fetch_github,
                                fetch_hackernews, article_lead, keyword_labels)
from news_digest import llm, wechat
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
                 patch('news_digest.__main__.collect_extras', return_value=([], [])), \
                 patch('news_digest.__main__.send', side_effect=['smtp_accepted', RuntimeError('failed')]) as delivery:
                self.assertEqual(main(), 1)
                self.assertEqual(delivery.call_count, 2)
            with patch.dict(os.environ, env), patch('sys.argv', ['digest']), \
                 patch('news_digest.__main__.validate_channels'), \
                 patch('news_digest.__main__.collect', return_value=([self.article()], [{'ok': True}])), \
                 patch('news_digest.__main__.collect_extras', return_value=([], [])), \
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


    def test_extras_delivered_to_configured_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'CHANNELS': 'email', 'STATE_PATH': str(Path(directory) / 's.json'),
                   'REPORT_DIR': directory, 'SMTP_USER': 'u', 'SMTP_PASSWORD': 'p',
                   'EMAIL_TO': 'to@example.com'}
            with patch.dict(os.environ, env), patch('sys.argv', ['digest']), \
                 patch('news_digest.__main__.validate_channels'), \
                 patch('news_digest.__main__.collect', return_value=([self.article()], [{'ok': True}])), \
                 patch('news_digest.__main__.collect_extras',
                       return_value=([self.article(title='论文标题')], [{'source': 'arXiv', 'ok': True}])), \
                 patch('news_digest.__main__.send', return_value='accepted') as delivery:
                self.assertEqual(main(), 0)
            # 主日报 1 次 + 专业资讯速递 1 次
            self.assertEqual(delivery.call_count, 2)
            self.assertTrue(any('专业资讯速递' in str(call.args[1]) for call in delivery.call_args_list))

    def test_unconfigured_channel_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'CHANNELS': 'email,pushplus,wecombot,wecom_app,telegram,serverchan',
                   'STATE_PATH': str(Path(directory) / 's.json'),
                   'REPORT_DIR': directory, 'PUSHPLUS_TOKEN': '', 'WECOM_WEBHOOK': '',
                   'WECOM_CORPID': '', 'WECOM_CORPSECRET': '', 'WECOM_AGENTID': '', 'WECOM_TOUSER': '',
                   'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': '', 'SERVERCHAN_SENDKEY': '',
                   'SMTP_USER': '', 'SMTP_PASSWORD': '', 'EMAIL_TO': ''}
            # 五个渠道全部缺凭证：send 应一次都不被调用且不抛错。
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

    def test_wecom_app_cards_batch_and_image_rules(self):
        articles = [self.article(title=f'标题{i}', summary='摘要内容', image=f'https://cdn.example.com/p{i}.jpg')
                    for i in range(10)]
        articles[2].image = 'http://insecure.example.com/x.jpg'  # 非 https 应被丢弃
        messages = wecom_app_cards(articles)
        self.assertEqual(len(messages), 2)  # 10 条分两批，每批 ≤8
        self.assertEqual(len(messages[0]['news']['articles']), 8)
        self.assertEqual(len(messages[1]['news']['articles']), 2)
        self.assertNotIn('picurl', messages[0]['news']['articles'][2])  # 非 https 无图
        self.assertEqual(messages[0]['news']['articles'][0]['picurl'], 'https://cdn.example.com/p0.jpg')

    @patch('news_digest.delivery.requests.post')
    @patch('news_digest.delivery.requests.get')
    def test_wecom_app_rejects_token_error(self, get, post):
        get.return_value.json.return_value = {'errcode': 40013}
        with patch.dict(os.environ, {'WECOM_CORPID': 'c', 'WECOM_CORPSECRET': 's',
                                     'WECOM_AGENTID': '1', 'WECOM_TOUSER': 'u'}):
            with self.assertRaises(RuntimeError):
                send('wecom_app', 't', 'html', 'plain', articles=[self.article()])
            post.assert_not_called()

    @patch('news_digest.delivery.requests.post')
    @patch('news_digest.delivery.requests.get')
    def test_wecom_app_sends_news_cards(self, get, post):
        get.return_value.json.return_value = {'errcode': 0, 'access_token': 'TOK'}
        post.return_value.json.return_value = {'errcode': 0, 'msgid': 'M1'}
        with patch.dict(os.environ, {'WECOM_CORPID': 'c', 'WECOM_CORPSECRET': 's',
                                     'WECOM_AGENTID': '1', 'WECOM_TOUSER': 'ding'}):
            articles = [self.article(title=f'标题{i}', summary='摘要内容', image=f'https://cdn.example.com/p{i}.jpg')
                        for i in range(3)]
            receipt = send('wecom_app', 't', 'html', 'plain', articles=articles)
        self.assertEqual(receipt, 'wecom_app_accepted:M1')
        self.assertIn('message/send', post.call_args.args[0])
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['msgtype'], 'news')
        self.assertEqual(payload['touser'], 'ding')
        self.assertEqual(len(payload['news']['articles']), 3)
        self.assertEqual(payload['news']['articles'][0]['picurl'], 'https://cdn.example.com/p0.jpg')

    @patch('news_digest.delivery.requests.post')
    def test_wecombot_sends_news_cards(self, post):
        post.return_value.json.return_value = {'errcode': 0, 'msgid': 'M2'}
        with patch.dict(os.environ, {'WECOM_WEBHOOK': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc'}):
            articles = [self.article(title=f'标题{i}', summary='摘要内容', image=f'https://cdn.example.com/p{i}.jpg')
                        for i in range(3)]
            receipt = send('wecombot', 't', 'html', 'plain', articles=articles)
        self.assertEqual(receipt, 'wecombot_accepted:M2')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['msgtype'], 'news')
        self.assertEqual(len(payload['news']['articles']), 3)
        self.assertEqual(payload['news']['articles'][0]['picurl'], 'https://cdn.example.com/p0.jpg')

    def test_pushplus_html_embeds_https_images_only(self):
        articles = [self.article(title='带图新闻', summary='摘要', image='https://cdn.example.com/p.jpg'),
                    self.article(title='无图新闻', summary='另一条', image=''),
                    self.article(title='http图被忽略', summary='x', image='http://insecure.example.com/p.jpg')]
        content = pushplus_html('AI 每日新闻 测试', articles)
        self.assertIn('<img src="https://cdn.example.com/p.jpg"', content)
        self.assertNotIn('http://insecure.example.com/p.jpg', content)
        self.assertNotIn('<img src=""', content)
        # HTML 转义防注入：标题里的尖括号不应破坏结构。
        evil = [self.article(title='<script>x</script>', summary='a', image='')]
        self.assertIn('&lt;script&gt;', pushplus_html('t', evil))

    @patch('news_digest.delivery.requests.post')
    def test_pushplus_send_uses_html_template_with_images(self, post):
        post.return_value.json.return_value = {'code': 200, 'data': 'receipt'}
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'tok'}):
            articles = [self.article(title='标题', summary='摘要', image='https://cdn.example.com/p.jpg')]
            receipt = send('pushplus', 't', 'html', 'plain', articles=articles)
        self.assertEqual(receipt, 'pushplus_queued:receipt')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['template'], 'html')
        self.assertIn('<img src="https://cdn.example.com/p.jpg"', payload['content'])

    def test_telegram_html_escapes_and_truncates(self):
        articles = [self.article(title='<b>标题</b>', summary='摘要', image='https://cdn.example.com/p.jpg'),
                    self.article(title='第二条', summary='x' * 700, image='')]
        content = telegram_html('AI 每日新闻 测试', articles)
        self.assertIn('&lt;b&gt;标题&lt;/b&gt;', content)  # 标题 HTML 转义
        self.assertIn('<a href="https://example.com/a"', content)  # 链接用 canonical_url
        self.assertNotIn('cdn.example.com/p.jpg', content)  # 配图走 sendPhoto，不在文本
        self.assertNotIn('x' * 700, content)  # 摘要超 600 截断
        self.assertIn('x' * 600, content)

    @patch('news_digest.delivery.requests.post')
    def test_telegram_send_splits_and_sends_photos(self, post):
        post.return_value.json.return_value = {'ok': True, 'result': {'message_id': 7}}
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'tok', 'TELEGRAM_CHAT_ID': '123'}):
            articles = [self.article(title='带图', summary='s', image='https://cdn.example.com/p.jpg'),
                        self.article(title='无图', summary='t', image='')]
            receipt = send('telegram', 't', 'html', 'plain', articles=articles)
        self.assertTrue(receipt.startswith('telegram_accepted:'))
        # 1 条文本（sendMessage）+ 1 张配图（sendPhoto）
        urls = [c.args[0] for c in post.call_args_list]
        self.assertEqual(sum('sendMessage' in u for u in urls), 1)
        self.assertEqual(sum('sendPhoto' in u for u in urls), 1)

    def test_serverchan_markdown_embeds_https_images_only(self):
        articles = [self.article(title='带图新闻', summary='摘要', image='https://cdn.example.com/p.jpg'),
                    self.article(title='无图新闻', summary='另一条', image=''),
                    self.article(title='http图被忽略', summary='x', image='http://insecure.example.com/p.jpg')]
        content = serverchan_markdown('AI 每日新闻 测试', articles)
        self.assertIn('**1. [带图新闻](https://example.com/a)**', content)  # canonical_url 归一
        self.assertIn('![](https://cdn.example.com/p.jpg)', content)
        self.assertNotIn('http://insecure.example.com/p.jpg', content)  # 仅 https 配图
        self.assertIn('## AI 每日新闻 测试', content)

    @patch('news_digest.delivery.requests.post')
    def test_serverchan_send_posts_markdown_and_caps_title(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'code': 0, 'data': {'pushid': 'P1'}}
        with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCT123'}):
            articles = [self.article(title='标题', summary='摘要', image='https://cdn.example.com/p.jpg')]
            receipt = send('serverchan', 'AI 每日新闻 2026-09-29' + '超长后缀' * 10,
                           'html', 'plain', articles=articles)
        self.assertEqual(receipt, 'serverchan_accepted:P1')
        self.assertEqual(post.call_args.args[0], 'https://sctapi.ftqq.com/SCT123.send')
        data = post.call_args.kwargs['data']
        self.assertLessEqual(len(data['title']), 32)  # Server酱标题上限 32 字符
        self.assertIn('![](https://cdn.example.com/p.jpg)', data['desp'])

    @patch('news_digest.delivery.requests.post')
    def test_serverchan_rejects_nonzero_code(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'code': 40001, 'message': 'bad key'}
        with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCT123'}):
            with self.assertRaises(RuntimeError):
                send('serverchan', 't', 'html', 'plain')

    def test_serverchan_markdown_caps_total_size(self):
        # RSS 原文摘要可达数千字：实测 12 条原始摘要拼出 126KB，远超 Server酱 32KB 上限被拒。
        # 渲染必须逐级收紧摘要并按字节兜底，保证能发出去且仍保留条目。
        articles = [self.article(title=f'长摘要第{i}条', summary='很长的中文摘要内容' * 400,
                                 image='https://cdn.example.com/p.jpg') for i in range(1, 13)]
        content = serverchan_markdown('AI 每日新闻 测试', articles)
        self.assertLessEqual(len(content.encode('utf-8')), 30000)
        self.assertIn('**1. [长摘要第1条', content)
        self.assertIn('![](https://cdn.example.com/p.jpg)', content)
        self.assertLessEqual(len(content), 30000)

    @patch('news_digest.delivery.requests.post')
    def test_serverchan_reports_http_status_without_leaking_key(self, post):
        # 非 200 时抛 RuntimeError 并带上状态码便于诊断；不得把含 SendKey 的 URL 写进异常信息。
        post.return_value.status_code = 413
        with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCT123'}):
            with self.assertRaises(RuntimeError) as ctx:
                send('serverchan', 't', 'html', 'plain')
        self.assertIn('413', str(ctx.exception))
        self.assertNotIn('SCT123', str(ctx.exception))

    @patch('news_digest.delivery.time.sleep')
    @patch('news_digest.delivery.requests.post')
    def test_serverchan_retries_transient_connection_error(self, post, _sleep):
        # Actions 实测：首次 TLS 连接偶发被重置（ConnectionError），同一运行内重试即可成功。
        ok = Mock()
        ok.status_code = 200
        ok.json.return_value = {'code': 0, 'data': {'pushid': 'P2'}}
        post.side_effect = [requests.exceptions.ConnectionError('reset'), ok]
        with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCT123'}):
            receipt = send('serverchan', 't', 'html', 'plain')
        self.assertEqual(receipt, 'serverchan_accepted:P2')
        self.assertEqual(post.call_count, 2)

    @patch('news_digest.delivery.time.sleep')
    @patch('news_digest.delivery.requests.post')
    def test_serverchan_does_not_retry_business_error(self, post, _sleep):
        # 业务错误码（额度用尽 / 标题非法）不应重试，避免白白消耗每日免费额度。
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'code': 40001, 'message': 'bad key'}
        with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCT123'}):
            with self.assertRaises(RuntimeError):
                send('serverchan', 't', 'html', 'plain')
        self.assertEqual(post.call_count, 1)

    def test_serverchan_url_switches_endpoint_by_key_type(self):
        self.assertEqual(serverchan_url('SCT123'), 'https://sctapi.ftqq.com/SCT123.send')
        self.assertEqual(serverchan_url('sctp42tABCD'),
                         'https://42.push.ft07.com/send/sctp42tABCD.send')

    def test_fetch_arxiv_filters_by_keyword_and_strips_prefix(self):
        atom = (b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom"><channel>'
                b'<item><title>Embodied Robot Learning in Simulation</title>'
                b'<link>https://arxiv.org/abs/2509.11111</link>'
                b'<description>arXiv:2509.11111v1 Announce Type: new\nAbstract: We study embodied manipulation.</description>'
                b'<arxiv:announce_type>new</arxiv:announce_type>'
                b'<pubDate>Mon, 28 Sep 2026 00:00:00 -0400</pubDate></item>'
                b'<item><title>Cooking Recipes for Fun</title>'
                b'<link>https://arxiv.org/abs/2509.22222</link>'
                b'<description>arXiv:2509.22222v1 Announce Type: new\nAbstract: unrelated cooking content.</description>'
                b'<arxiv:announce_type>new</arxiv:announce_type>'
                b'<pubDate>Mon, 28 Sep 2026 00:00:00 -0400</pubDate></item>'
                b'</channel></rss>')
        client, response = Mock(), Mock()
        response.content, response.raise_for_status.return_value = atom, None
        client.get.return_value = response
        papers = fetch_arxiv(client, {'categories': ['cs.RO'], 'keywords': ['embodied', 'robot learning'],
                                      'max_results': 5, 'translate': False},
                             datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(len(papers), 1)  # 只保留命中关键词的一条
        self.assertEqual(papers[0].source, 'arXiv 论文')
        self.assertIn('embodied manipulation', papers[0].summary)
        self.assertNotIn('Announce Type', papers[0].summary)  # 前缀已剥掉
        # 命中的词要随条目带出来，供上屏显示（否则速递里关键词一栏是空的），
        # 且转成中文标签——匹配词是英文，但展示要给人看懂。
        self.assertEqual(papers[0].keywords, ['具身智能', '机器人学习'])

    def test_keyword_labels_keeps_unknown_and_dedupes(self):
        # 未收录的词原样输出（新增关键词不会丢），同义匹配词映射后去重。
        self.assertEqual(keyword_labels(['sim-to-real', 'sim2real', 'brand-new-term']),
                         ['仿真到现实', 'brand-new-term'])

    ARXIV_ATOM = (b'<rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom"><channel>'
                  b'<item><title>Embodied Robot Learning in Simulation</title>'
                  b'<link>https://arxiv.org/abs/2509.11111</link>'
                  b'<description>arXiv:2509.11111v1 Announce Type: new\nAbstract: We study embodied manipulation.</description>'
                  b'<arxiv:announce_type>new</arxiv:announce_type>'
                  b'<pubDate>Mon, 28 Sep 2026 00:00:00 -0400</pubDate></item>'
                  b'<item><title>Cooking Recipes for Fun</title>'
                  b'<link>https://arxiv.org/abs/2509.22222</link>'
                  b'<description>arXiv:2509.22222v1 Announce Type: new\nAbstract: unrelated cooking content.</description>'
                  b'<arxiv:announce_type>new</arxiv:announce_type>'
                  b'<pubDate>Mon, 28 Sep 2026 00:00:00 -0400</pubDate></item>'
                  b'</channel></rss>')

    def _arxiv_client(self, atom=None):
        client, response = Mock(), Mock()
        response.content = atom or self.ARXIV_ATOM
        response.raise_for_status.return_value = None
        client.get.return_value = response
        return client

    @patch('news_digest.extras.llm.digest_zh',
           return_value=('仿真环境中学习具身操作', '解决仿真到现实的迁移问题。方法用 VLA 模型。'))
    def test_fetch_arxiv_translates_to_chinese(self, _digest):
        # 丁老师要求：标题用英文原文，译文另起一行跟在后面（不再替换原标题）。
        papers = fetch_arxiv(self._arxiv_client(),
                             {'categories': ['cs.RO'], 'keywords': ['embodied'],
                              'max_results': 5, 'translate': True},
                             datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, 'Embodied Robot Learning in Simulation')  # 原文保留
        self.assertEqual(papers[0].subtitle, '仿真环境中学习具身操作')  # 译文单独放
        self.assertIn('仿真到现实', papers[0].summary)
        self.assertNotIn('embodied manipulation', papers[0].summary)  # 不再抛整段英文摘要

    @patch('news_digest.extras.llm.digest_zh', return_value=('', ''))
    def test_fetch_arxiv_drops_english_abstract_when_llm_unavailable(self, _digest):
        # 中文速读不可用时，降级为「英文标题 + 链接」，不留整段英文摘要。
        papers = fetch_arxiv(self._arxiv_client(),
                             {'categories': ['cs.RO'], 'keywords': ['embodied'],
                              'max_results': 5, 'translate': True},
                             datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(papers[0].title, 'Embodied Robot Learning in Simulation')
        self.assertEqual(papers[0].subtitle, '')  # 无译文就没有第二行
        self.assertEqual(papers[0].summary, '')

    def test_article_lead_strips_boilerplate(self):
        page = ('<div class="article">标题 梦晨 2026-09-29 08:49:30 来源： 量子位 '
                '李飞飞将入职AMD 梦晨 发自 凹非寺 量子位 | 公众号 QbitAI '
                '82亿美元，AMD全股票收购World Labs。后续正文内容。</div>'
                '<div class="article_info">meta</div>')
        lead = article_lead(page, '标题')
        self.assertTrue(lead.startswith('82亿美元'))
        self.assertNotIn('公众号', lead)
        self.assertEqual(article_lead('<html>没有正文容器</html>', '标题'), '')

    def test_fetch_chinese_filters_and_respects_title_only(self):
        def build(items):
            body = b''.join(
                f'<item><title>{t}</title><link>{u}</link><description>{d}</description>'
                f'<pubDate>Mon, 28 Sep 2026 00:00:00 +0000</pubDate></item>'.encode()
                for t, u, d in items)
            return b'<rss version="2.0"><channel>' + body + b'</channel></rss>'

        qbit = build([('人形机器人新进展', 'https://www.qbitai.com/2026/09/a.html', '一句话导语'),
                      ('办公室装修指南', 'https://www.qbitai.com/2026/09/b.html', '与智能无关')])
        solidot = build([('小偷想偷英伟达芯片结果偷了20吨沙子',
                          'https://www.solidot.org/story?sid=1', '正文里提到了自动驾驶'),
                         ('AI 生成内容的版权之争', 'https://www.solidot.org/story?sid=2', '摘要')])
        client = Mock()

        def respond(url, **kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            response.content = qbit if 'qbitai' in url else solidot
            return response

        client.get.side_effect = respond
        items = fetch_chinese(client, {
            'days': 30, 'max_results': 5, 'keywords': ['机器人', 'AI'],
            'feeds': [{'name': '量子位', 'url': 'https://www.qbitai.com/feed', 'max_results': 3},
                      {'name': 'Solidot 奇客', 'url': 'https://www.solidot.org/index.rss',
                       'max_results': 3, 'title_only': True}]},
            datetime(2026, 9, 29, tzinfo=timezone.utc))
        titles = [a.title for a in items]
        self.assertIn('人形机器人新进展', titles)
        self.assertIn('AI 生成内容的版权之争', titles)
        self.assertNotIn('办公室装修指南', titles)  # 关键词不命中
        self.assertNotIn('小偷想偷英伟达芯片结果偷了20吨沙子', titles)  # 关键词只在摘要里

    @patch('news_digest.llm.requests.post')
    def test_llm_digest_zh_strips_labels_and_parses(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'choices': [{'message': {'content':
            '中文标题：具身操作新方法\n速读：解决跨场景迁移问题。用 VLA 模型统一表征。'}}]}
        with patch.dict(os.environ, {'LLM_API_KEY': 'sk-test'}):
            title, body = llm.digest_zh('English Title', 'abstract')
        self.assertEqual(title, '具身操作新方法')  # 「中文标题：」前缀已剥掉
        self.assertIn('跨场景迁移', body)
        self.assertTrue(post.call_args.kwargs['headers']['Authorization'].startswith('Bearer '))
        self.assertNotIn('sk-test', post.call_args.args[0])  # 密钥不进 URL

    def test_llm_disabled_without_key(self):
        with patch.dict(os.environ, {'LLM_API_KEY': ''}):
            self.assertFalse(llm.enabled())
            self.assertEqual(llm.digest_zh('t', 'a'), ('', ''))

    @patch('news_digest.llm.requests.post')
    def test_llm_empty_env_falls_back_to_defaults(self, post):
        # Actions 里未配置的 secret 会展开成空字符串：必须回落到默认地址与模型，
        # 否则会拼出 '/chat/completions' 这种残缺 URL 而静默失败。
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'choices': [{'message': {'content': '标题\n速读'}}]}
        with patch.dict(os.environ, {'LLM_API_KEY': 'sk-test', 'LLM_BASE_URL': '', 'LLM_MODEL': ''}):
            llm.digest_zh('t', 'a')
        self.assertEqual(post.call_args.args[0], 'https://api.siliconflow.cn/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['json']['model'], 'THUDM/GLM-4-9B-0414')

    def test_render_puts_subtitle_on_its_own_line(self):
        # 丁老师要求：标题保留英文原文，中文译文另起一行跟在后面。
        page, plain = render([self.article(title='GT-VLA: Target-Conditioned Trace Guidance',
                                           subtitle='GT-VLA：目标条件轨迹引导')],
                             '专业资讯速递', 100)
        self.assertIn('<p class="subtitle">GT-VLA：目标条件轨迹引导</p>', page)
        self.assertIn('<h2>1. <a href=', page)
        self.assertIn('GT-VLA: Target-Conditioned Trace Guidance', page)  # 原文仍在
        # 纯文本：标题行 + 两空格缩进的译文行，供 wecom_markdown 反解析。
        self.assertIn('\n1. GT-VLA: Target-Conditioned Trace Guidance\n  GT-VLA：目标条件轨迹引导\n', plain)

    def test_render_omits_subtitle_line_when_absent(self):
        # 中文源条目本来就没有译文，不该凭空多出一行。
        _, plain = render([self.article(title='李飞飞创业公司被收购', summary='摘要')], '速递', 100)
        self.assertNotIn('  \n', plain)

    def test_render_omits_keyword_line_when_empty(self):
        # 丁老师反馈「关键词：」后面是空的。速递条目未必带关键词，空标签必须整行消失。
        page, _ = render([self.article(title='论文', summary='速读')], '速递', 100)
        self.assertNotIn('关键词', page)

    def test_render_shows_matched_keywords(self):
        # 有命中关键词时要真的显示出来，而不是被一并藏掉。
        page, _ = render([self.article(title='论文', summary='速读',
                                       keywords=['具身', '机器人'])], '速递', 100)
        self.assertIn('关键词：具身 / 机器人', page)

    def test_subtitle_reaches_serverchan_telegram_pushplus(self):
        article = self.article(title='Humanoid Badminton', subtitle='人形机器人打羽毛球')
        markdown = serverchan_markdown('速递', [article])
        self.assertIn('**1. [Humanoid Badminton](https://example.com/a)**', markdown)
        self.assertIn('人形机器人打羽毛球', markdown)
        telegram = telegram_html('速递', [article])
        self.assertIn('人形机器人打羽毛球', telegram)
        pushplus = pushplus_html('速递', [article])
        self.assertIn('人形机器人打羽毛球', pushplus)

    def test_wecom_app_card_puts_subtitle_first_in_description(self):
        article = self.article(title='Humanoid Badminton', subtitle='人形机器人打羽毛球',
                               summary='用有限人体动作数据学习挥拍。')
        description = wecom_app_cards([article])[0]['news']['articles'][0]['description']
        self.assertTrue(description.startswith('人形机器人打羽毛球'))
        self.assertIn('挥拍', description)

    def test_wecom_markdown_does_not_mistake_subtitle_for_summary(self):
        # 回归防护：wecom_markdown 从纯文本反解析，「紧跟标题的第一行长文本即摘要」。
        # 译文行也是长文本，若无缩进分支就会顶掉真正的摘要。
        page, plain = render([self.article(title='GT-VLA: Trace Guidance',
                                           subtitle='GT-VLA：轨迹引导方法',
                                           summary='这是真正的摘要内容。')], '速递', 100)
        self.assertIn('GT-VLA：轨迹引导方法', page)  # HTML 版同样带译文
        markdown = wecom_markdown('速递', plain)
        self.assertIn('GT-VLA：轨迹引导方法', markdown)
        self.assertIn('这是真正的摘要内容。', markdown)

    def test_render_note_overrides_default(self):
        page, plain = render([self.article(title='标题', summary='摘要')], '每日速递', 100,
                             note='arXiv 条目由大模型改写为中文速读。')
        self.assertIn('大模型改写', plain)
        self.assertIn('大模型改写', page)
        self.assertNotIn('未调用大模型', page)  # extras 不得谎称未用大模型

    def test_llm_stats_distinguish_success_from_silent_failure(self):
        # 线上只靠「日志里没有 WARNING」判断不了中文速读有没有生效：
        # chat 有静默返回空串的路径（无 key），失败还可能发生在响应解析阶段。
        # 统计必须把成功与各种失败分开计数，并留下首个失败原因。
        llm.reset_stats()
        with patch.dict(os.environ, {'LLM_API_KEY': ''}):
            llm.digest_zh('t', 'a')
        self.assertEqual(llm.stats(), {'ok': 0, 'fail': 1, 'reason': 'no_api_key'})

        llm.reset_stats()
        with patch('news_digest.llm.requests.post') as post:
            post.return_value.status_code = 401
            with patch.dict(os.environ, {'LLM_API_KEY': 'sk-test'}):
                llm.digest_zh('t', 'a')
        self.assertEqual(llm.stats(), {'ok': 0, 'fail': 1, 'reason': 'http_401'})

        llm.reset_stats()
        with patch('news_digest.llm.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'choices': [{'message': {'content': '中文标题\n速读正文'}}]}
            with patch.dict(os.environ, {'LLM_API_KEY': 'sk-test'}):
                llm.digest_zh('t', 'a')
        self.assertEqual(llm.stats(), {'ok': 1, 'fail': 0, 'reason': ''})

    def test_llm_stats_records_model_returning_empty_body(self):
        # 模型返回 200 但内容为空：算失败，且原因可辨，不能计入成功。
        llm.reset_stats()
        with patch('news_digest.llm.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'choices': [{'message': {'content': '   '}}]}
            with patch.dict(os.environ, {'LLM_API_KEY': 'sk-test'}):
                self.assertEqual(llm.digest_zh('t', 'a'), ('', ''))
        self.assertEqual(llm.stats(), {'ok': 0, 'fail': 1, 'reason': 'empty_output'})

    @patch('news_digest.llm.requests.post')
    @patch('news_digest.extras.session')
    def test_collect_extras_reports_llm_status_row(self, _session, post):
        # 「中文速读」单列一行状态，Actions 日志与 status.json 都能直接看出是否跑通。
        # 这里 mock 的是底层 HTTP，让真实的 digest_zh 跑一遍，统计才有意义。
        client = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.content = ('<rss version="2.0"><channel><item><title>Robot learning with VLA</title>'
                            '<link>https://arxiv.org/abs/2509.00001</link>'
                            '<description>arXiv:2509.00001 Announce Type: new\nAbstract: '
                            'embodied manipulation, sim-to-real transfer.</description>'
                            '<pubDate>Mon, 29 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>')
        client.get.return_value = response
        _session.return_value.__enter__ = Mock(return_value=client)
        _session.return_value.__exit__ = Mock(return_value=False)

        config = {'extras': {'arxiv': {'enabled': True, 'translate': True, 'max_results': 1,
                                       'categories': ['cs.RO'], 'keywords': ['VLA']},
                             'chinese': {'enabled': False}}}
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'choices': [{'message': {
            'content': '具身操作新方法\n解决跨场景迁移问题，用 VLA 统一表征。'}}]}
        with patch.dict(os.environ, {'LLM_API_KEY': 'sk-test'}):
            articles, status = collect_extras(config, datetime.now(timezone.utc))
        rows = {row['source']: row for row in status}
        self.assertEqual(rows['中文速读']['ok'], True)
        self.assertEqual(rows['中文速读']['count'], 1)
        self.assertEqual(rows['中文速读']['failed'], 0)
        self.assertEqual(articles[0].title, 'Robot learning with VLA')  # 原文
        self.assertEqual(articles[0].subtitle, '具身操作新方法')  # 译文

    @patch('news_digest.extras.session')
    def test_collect_extras_reports_llm_failure_reason(self, _session):
        # 速读全失败时，状态行要给出 ok=False 与首个失败原因，便于线上定位。
        client = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.content = ('<rss version="2.0"><channel><item><title>Robot learning with VLA</title>'
                            '<link>https://arxiv.org/abs/2509.00002</link>'
                            '<description>arXiv:2509.00002 Announce Type: new\nAbstract: '
                            'embodied manipulation.</description>'
                            '<pubDate>Mon, 29 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>')
        client.get.return_value = response
        _session.return_value.__enter__ = Mock(return_value=client)
        _session.return_value.__exit__ = Mock(return_value=False)

        config = {'extras': {'arxiv': {'enabled': True, 'translate': True, 'max_results': 1,
                                       'categories': ['cs.RO'], 'keywords': ['VLA']},
                             'chinese': {'enabled': False}}}
        with patch.dict(os.environ, {'LLM_API_KEY': ''}):  # 未配置密钥：必然降级
            articles, status = collect_extras(config, datetime.now(timezone.utc))
        rows = {row['source']: row for row in status}
        self.assertFalse(rows['中文速读']['ok'])
        self.assertEqual(rows['中文速读']['count'], 0)
        self.assertEqual(rows['中文速读']['reason'], 'no_api_key')
        # 降级后仍要有条目，只是空摘要——速读失败不能把论文丢掉。
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].summary, '')
        self.assertEqual(articles[0].subtitle, '')  # 无译文也不该有第二行

    def test_fetch_github_shapes_and_truncates(self):
        client, response = Mock(), Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'items': [{
            'full_name': 'a/b', 'html_url': 'https://github.com/a/b', 'description': 'desc',
            'topics': ['ai', 'llm'], 'created_at': '2026-09-25T00:00:00Z', 'stargazers_count': 321}]}
        client.get.return_value = response
        repos = fetch_github(client, {'days': 7, 'min_stars': 100, 'max_results': 5},
                             datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0].title, 'a/b')
        self.assertEqual(repos[0].source, 'GitHub 趋势项目')
        self.assertIn('ai', repos[0].summary)

    def test_fetch_hackernews_builds_articles(self):
        client = Mock()

        def respond(url, **kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if 'topstories' in url:
                response.json.return_value = [1, 2, 3]
            else:
                story_id = url.rsplit('/', 1)[1].split('.')[0]
                response.json.return_value = {'title': f'Story {story_id}',
                                              'url': f'https://news.example.com/{story_id}',
                                              'time': 1758000000}
            return response

        client.get.side_effect = respond
        stories = fetch_hackernews(client, {'max_results': 2}, datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(len(stories), 2)  # 只取前 2 条
        self.assertEqual(stories[0].source, 'Hacker News')
        self.assertEqual(stories[0].title, 'Story 1')

    @patch('news_digest.extras.session')
    def test_collect_extras_survives_partial_failure(self, _session):
        with patch('news_digest.extras.fetch_arxiv', side_effect=RuntimeError('429 throttled')), \
             patch('news_digest.extras.fetch_github', return_value=[self.article()]), \
             patch('news_digest.extras.fetch_hackernews', return_value=[]):
            articles, status = collect_extras(
                {'extras': {'arxiv': {'enabled': True}, 'github': {'enabled': True},
                            'hackernews': {'enabled': True}}},
                datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(len(articles), 1)  # 单源失败不影响其余来源
        self.assertTrue(any(s['ok'] is False for s in status))
        self.assertTrue(any(s['ok'] is True for s in status))

    def test_extract_image_prefers_media_and_validates(self):
        media_entry = {'media_content': [{'url': 'https://cdn.example.com/pic'}],
                       'summary': '<img src="https://x.example.com/a.jpg">'}
        self.assertEqual(extract_image(media_entry), 'https://cdn.example.com/pic')
        body_entry = {'summary': '<p><img src="https://x.example.com/a.png"></p>'}
        self.assertEqual(extract_image(body_entry), 'https://x.example.com/a.png')
        bad_entry = {'summary': '<img src="javascript:alert(1)"><img src="/relative.png">'}
        self.assertEqual(extract_image(bad_entry), '')
        self.assertEqual(extract_image({}), '')


class WechatDraftTests(unittest.TestCase):
    """公众号草稿箱：只做草稿是硬约束（个人主体账号的发布接口已被微信回收）。"""

    def setUp(self):
        self.now = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
        self.article = Article('Humanoid Badminton: Dynamic Racket Skills',
                               'https://arxiv.org/abs/2509.00001',
                               '用有限的人体动作数据学习动态挥拍技能。', self.now,
                               'arXiv 论文', 0, '论文', subtitle='人形机器人打羽毛球')

    def test_entry_keeps_original_title_and_subtitle_on_separate_lines(self):
        markup = wechat.entry_html(1, self.article)
        self.assertIn('Humanoid Badminton', markup)
        self.assertIn('人形机器人打羽毛球', markup)
        # 原文与译题必须是两个独立段落，不能挤在同一行。
        self.assertEqual(markup.count('<p '), 4)  # 标题 / 译题 / 摘要 / 来源

    def test_entry_skips_subtitle_when_absent(self):
        plain = Article('量子位：某公司发布新模型', 'https://example.com/a', '摘要正文',
                        self.now, '量子位', 0, '资讯')
        self.assertEqual(wechat.entry_html(1, plain).count('<p '), 3)

    def test_entry_escapes_html(self):
        evil = Article('<script>alert(1)</script>', 'https://example.com/a',
                       '<img onerror=x>', self.now, '<b>src</b>', 0, '资讯')
        markup = wechat.entry_html(1, evil)
        self.assertNotIn('<script>', markup)
        self.assertNotIn('<img onerror', markup)
        self.assertIn('&lt;script&gt;', markup)

    def test_entry_drops_external_links(self):
        # 公众号正文里的外链会被微信过滤，放了也是纯文本 —— 干脆不生成 <a>。
        self.assertNotIn('<a ', wechat.entry_html(1, self.article))

    def test_section_renders_heading_and_numbers_items(self):
        markup = wechat.section_html('今日要闻', [self.article, self.article])
        self.assertIn('今日要闻', markup)
        self.assertIn('>1. ', markup)
        self.assertIn('>2. ', markup)

    def test_render_article_skips_empty_sections(self):
        content = wechat.render_article([('今日要闻', [self.article]), ('专业资讯速递', [])], note='说明')
        self.assertIn('今日要闻', content)
        self.assertNotIn('专业资讯速递', content)  # 空小节不落一个光杆标题

    def test_entry_caps_summary_length(self):
        # 中文媒体正文首段实测可到 2800+ 字，不设限的话 20 条能拼出 5.6 万字符直接超微信上限。
        long = Article('标题', 'https://example.com/a', '正' * 3000, self.now, '量子位', 0, '资讯')
        markup = wechat.entry_html(1, long)
        self.assertIn('正' * wechat.SUMMARY_CHARS, markup)
        self.assertNotIn('正' * (wechat.SUMMARY_CHARS + 1), markup)

    def test_render_article_drops_tail_entries_to_fit(self):
        # 放不下时从末尾丢条目，且绝不返回超限正文（微信会直接拒收）。
        items = [Article(f'标题{i}', f'https://example.com/{i}', '摘要' * 400,
                         self.now, 'test', 0, '资讯') for i in range(30)]
        content = wechat.render_article([('今日要闻', items)], note='说明')
        self.assertLessEqual(len(content), wechat.MAX_CONTENT_CHARS)
        self.assertIn('标题0', content)          # 头部的条目保住
        self.assertNotIn('标题29', content)      # 末尾的被丢
        self.assertEqual(len(items), 30)         # 不改动调用方的列表

    def test_render_article_prioritizes_main_news_over_extras(self):
        main = [Article(f'要闻{i}', f'https://example.com/m{i}', '正文' * 400,
                        self.now, 'test', 0, '新闻') for i in range(20)]
        extras = [Article(f'速递{i}', f'https://example.com/e{i}', '正文' * 400,
                          self.now, 'test', 0, '论文') for i in range(20)]
        content = wechat.render_article([('今日要闻', main), ('专业资讯速递', extras)], note='说明')
        self.assertLessEqual(len(content), wechat.MAX_CONTENT_CHARS)
        self.assertIn('要闻0', content)
        # 速递是补充内容，先被牺牲。
        self.assertNotIn('速递19', content)

    def test_clip_bytes_never_splits_a_chinese_char(self):
        clipped = wechat._clip_bytes('机' * 40, 60)
        self.assertLessEqual(len(clipped.encode('utf-8')), 60)
        clipped.encode('utf-8').decode('utf-8')  # 合法 UTF-8，不会出现半个汉字
        self.assertEqual(wechat._clip_bytes('短标题', 60), '短标题')

    def test_credentials_requires_both_values(self):
        with patch.dict(os.environ, {'WECHAT_APPID': '', 'WECHAT_SECRET': ''}, clear=False):
            with self.assertRaises(RuntimeError):
                wechat.credentials()
        with patch.dict(os.environ, {'WECHAT_APPID': 'wx1', 'WECHAT_SECRET': ''}, clear=False):
            with self.assertRaises(RuntimeError):
                wechat.credentials()

    def test_token_is_cached_and_bound_to_appid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'token.json'
            env = {'WECHAT_APPID': 'wx1', 'WECHAT_SECRET': 's1', 'WECHAT_TOKEN_CACHE': str(cache)}
            response = Mock()
            response.json.return_value = {'access_token': 'tok-1', 'expires_in': 7200}
            with patch.dict(os.environ, env, clear=False), \
                    patch('news_digest.wechat.requests.get', return_value=response) as get:
                self.assertEqual(wechat.access_token(), 'tok-1')
                self.assertEqual(wechat.access_token(), 'tok-1')  # 第二次走缓存
                self.assertEqual(get.call_count, 1)
            # 换了 AppID，旧 token 必须作废。
            response2 = Mock()
            response2.json.return_value = {'access_token': 'tok-2', 'expires_in': 7200}
            with patch.dict(os.environ, dict(env, WECHAT_APPID='wx2'), clear=False), \
                    patch('news_digest.wechat.requests.get', return_value=response2):
                self.assertEqual(wechat.access_token(), 'tok-2')

    def test_token_error_never_leaks_secret(self):
        response = Mock()
        response.json.return_value = {'errcode': 40164, 'errmsg': 'invalid ip 1.2.3.4'}
        with patch.dict(os.environ, {'WECHAT_APPID': 'wx1', 'WECHAT_SECRET': 'super-secret'},
                        clear=False), \
                patch('news_digest.wechat.requests.get', return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                wechat.access_token(force=True)
        message = str(ctx.exception)
        self.assertIn('40164', message)
        self.assertNotIn('super-secret', message)

    def test_add_draft_clips_title_and_digest(self):
        response = Mock()
        response.json.return_value = {'media_id': 'draft-1'}
        with patch.dict(os.environ, {'WECHAT_APPID': 'wx1', 'WECHAT_SECRET': 's1'}, clear=False), \
                patch('news_digest.wechat.access_token', return_value='tok'), \
                patch('news_digest.wechat.requests.post', return_value=response) as post:
            media_id = wechat.add_draft('AI 日报 | ' + '很长的标题' * 30, '<p>正文</p>', 'thumb-1',
                                        digest='摘要' * 200, author='丁老师')
        self.assertEqual(media_id, 'draft-1')
        payload = json.loads(post.call_args.kwargs['data'].decode('utf-8'))
        article = payload['articles'][0]
        self.assertLessEqual(len(article['title'].encode('utf-8')), wechat.MAX_TITLE_BYTES)
        self.assertEqual(len(article['digest']), wechat.MAX_DIGEST_CHARS)
        self.assertEqual(article['thumb_media_id'], 'thumb-1')  # 封面必填
        self.assertEqual(article['author'], '丁老师')

    def test_cover_falls_back_to_local_file_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            cover = Path(tmp) / 'cover.png'
            cover.write_bytes(b'\x89PNG fake bytes')
            cache = Path(tmp) / 'cover.json'
            env = {'WECHAT_COVER': str(cover), 'WECHAT_COVER_CACHE': str(cache),
                   'WECHAT_APPID': 'wx1', 'WECHAT_SECRET': 's1'}
            uploaded = Mock()
            uploaded.json.return_value = {'media_id': 'media-1'}
            with patch.dict(os.environ, env, clear=False), \
                    patch('news_digest.wechat.access_token', return_value='tok'), \
                    patch('news_digest.wechat.requests.post', return_value=uploaded) as post:
                self.assertEqual(wechat.cover_media_id([]), 'media-1')
                self.assertEqual(wechat.cover_media_id([]), 'media-1')  # 同图不重复上传
                self.assertEqual(post.call_count, 1)

    def test_cover_raises_when_nothing_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'WECHAT_COVER': '', 'WECHAT_COVER_CACHE': str(Path(tmp) / 'c.json')}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(RuntimeError) as ctx:
                    wechat.cover_media_id([self.article])  # 无配图且未指定本地封面
            self.assertIn('cover', str(ctx.exception))

    def test_push_draft_composes_sections_and_digest(self):
        extra = Article('量子位：某公司发布新模型', 'https://example.com/b', '中文摘要正文',
                        self.now, '量子位', 0, '资讯')
        with patch.dict(os.environ, {'WECHAT_SOURCE_URL': 'https://example.com/daily',
                                     'WECHAT_AUTHOR': '丁老师'}, clear=False), \
                patch('news_digest.wechat.cover_media_id', return_value='thumb-1'), \
                patch('news_digest.wechat.add_draft', return_value='draft-9') as add:
            media_id, title = wechat.push_draft([self.article], [extra], '2026年09月29日')
        self.assertEqual(media_id, 'draft-9')
        self.assertIn('2026年09月29日', title)
        # add_draft(title, content, thumb_media_id, digest=..., source_url=..., author=...)
        args, kwargs = add.call_args
        content = args[1]
        self.assertIn('今日要闻', content)
        self.assertIn('专业资讯速递', content)
        self.assertEqual(kwargs['source_url'], 'https://example.com/daily')
        self.assertLessEqual(len(kwargs['digest']), wechat.MAX_DIGEST_CHARS)


if __name__ == '__main__':
    unittest.main()

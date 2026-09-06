import calendar
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(value):
    parser = TextExtractor()
    parser.feed(str(value or ''))
    return re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()


def canonical_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username:
        raise ValueError('Invalid article URL')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    path = parts.path.rstrip('/')
    if parts.hostname in ('arxiv.org', 'export.arxiv.org'):
        path = re.sub(r'v\d+$', '', path.replace('/pdf/', '/abs/').removesuffix('.pdf'))
        return 'https://arxiv.org' + path
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(sorted(query)), ''))


def title_key(value):
    return re.sub(r'[^\w]', '', value.casefold())


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def matches(keyword, value):
    # Latin short words must not match inside unrelated words, e.g. AI in paid.
    pattern = re.escape(keyword)
    if keyword.isascii():
        pattern = r'(?<![a-z0-9])' + pattern + r'(?![a-z0-9])'
    return bool(re.search(pattern, value, re.IGNORECASE))


def parse_date(value):
    if isinstance(value, (tuple, list)):
        return datetime.fromtimestamp(calendar.timegm(value), timezone.utc)
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@dataclass
class Article:
    title: str
    url: str
    summary: str
    published: datetime
    source: str
    weight: int = 0
    kind: str = '新闻'
    stars: int = 0
    score: float = 0
    keywords: list = field(default_factory=list)

    @property
    def keys(self):
        return [digest(canonical_url(self.url)), digest(title_key(self.title))]


def chinese_readable(text):
    letters = re.findall(r'[a-zA-Z\u3400-\u9fff]', text)
    chinese = re.findall(r'[\u3400-\u9fff]', text)
    return len(chinese) >= 2 and len(chinese) / max(1, len(letters)) >= 0.2


def rank(articles, config, now):
    selected = []
    for article in articles:
        if config.get('chinese_only', False):
            excerpt = article.summary[:config['summary_chars']]
            if not chinese_readable(article.title) or (excerpt.strip() and not chinese_readable(excerpt)):
                continue
        if article.published is None:
            continue
        age = (now - article.published).total_seconds() / 3600
        if age < -1 or age > config['lookback_hours']:
            continue
        content = article.title + ' ' + article.summary
        if any(matches(k, content) for k in config['exclude_keywords']):
            continue
        article.keywords = [k for k in config['keywords'] if matches(k, content)]
        if not article.keywords:
            continue
        article.score = article.weight + min(12, sum(config['keywords'][k] for k in article.keywords))
        article.score += 2 if age <= 24 else 1
        article.score += min(3, article.stars // 1000)
        if article.score >= config['min_score']:
            selected.append(article)
    selected.sort(key=lambda a: (a.score, a.published), reverse=True)
    unique = []
    for article in selected:
        if any(set(article.keys) & set(other.keys) or
               SequenceMatcher(None, title_key(article.title), title_key(other.title)).ratio()
               >= config['title_similarity'] for other in unique):
            continue
        unique.append(article)
    return unique


def load_state(path):
    if not Path(path).exists():
        return {'version': 1, 'channels': {}}
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if value.get('version') != 1 or not isinstance(value.get('channels'), dict):
        raise ValueError('Invalid state; refusing to resend everything')
    return value


def save_state(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')
    temporary.replace(path)


def pending(articles, state, channel, limit):
    known = state['channels'].get(channel, {})
    return [a for a in articles if not any(k in known for k in a.keys)][:limit]


def mark(state, channel, articles, now):
    known = state['channels'].setdefault(channel, {})
    for article in articles:
        for key in article.keys:
            known[key] = now.isoformat()
    # Keep 90 days, much longer than the collection window.
    state['channels'][channel] = {k: v for k, v in known.items()
                                   if (now - parse_date(v)).days < 90}


def render(articles, title, summary_chars=280):
    esc = html.escape
    blocks, lines = [], [title, '规则筛选；摘要为来源原文摘录，未调用大模型。', '']
    for index, article in enumerate(articles, 1):
        summary = article.summary[:summary_chars]
        meta = f'{article.kind} · {article.source} · {article.published:%Y-%m-%d %H:%M UTC} · 分数 {article.score:g}'
        hits = ' / '.join(article.keywords)
        blocks.append(f'<article><h2>{index}. <a href="{esc(canonical_url(article.url), quote=True)}">{esc(article.title)}</a></h2>'
                      f'<p class="meta">{esc(meta)}</p><p>{esc(summary)}</p><p class="meta">关键词：{esc(hits)}</p></article>')
        lines.extend([f'{index}. {article.title}', meta, summary, article.url, ''])
    if not articles:
        blocks.append('<p>本次没有符合条件的新闻。</p>')
        lines.append('本次没有符合条件的新闻。')
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    page += f'<title>{esc(title)}</title><style>body{{max-width:820px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui;color:#243247;background:#f5f7fb}}article{{background:white;padding:18px;margin:16px 0;border-radius:12px}}h2{{font-size:20px}}a{{color:#175ca6}}.meta{{color:#657487;font-size:13px}}</style><body>'
    page += f'<h1>{esc(title)}</h1><p>规则筛选 · 来源原文摘录 · 不使用大模型 API</p>' + ''.join(blocks) + '</body></html>'
    return page, '\n'.join(lines)

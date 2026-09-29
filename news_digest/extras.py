"""每日「专业资讯速递」：arXiv 具身智能论文 + GitHub 趋势项目 + Hacker News 热帖。

选型约束：本模块运行在 GitHub Actions（境外机房），只使用免费、境外可达的公开数据源
（arXiv Atom API、GitHub Search API、HN Firebase API），不依赖任何需管理员/建群/付费的通道。

产出统一转成 core.Article，复用 core.render 与 delivery.telegram_html 的渲染/推送逻辑。
任一数据源失败只记录状态、不影响其余来源，也不会让整个日报任务失败。
"""

import re
from datetime import datetime, timedelta, timezone

import feedparser

from .core import Article, canonical_url, clean, matches, parse_date
from .sources import session

# arXiv 分类 RSS 摘要前缀形如「arXiv:2509.xxxxx Announce Type: new\nAbstract: ...」，需剥掉。
ARXIV_PREFIX = re.compile(r'^arXiv:\S+\s+Announce Type:\s*\w+\s*Abstract:\s*', re.IGNORECASE)

# 具身智能 / 机器人方向关键词，对齐丁老师的教学与研究方向。
DEFAULT_ARXIV_KEYWORDS = ['embodied', 'vision-language-action', 'VLA', 'robot learning', 'humanoid',
                          'sim-to-real', 'sim2real', 'manipulation', 'legged', 'grasping',
                          'imitation learning', 'world model', 'diffusion policy']

ARXIV_RSS = 'https://rss.arxiv.org/rss/'


def fetch_arxiv(client, config, now):
    """arXiv 当日新论文（分类 RSS + 关键词过滤）。

    用分类 RSS（rss.arxiv.org）而非 search API——API 对共享 IP 频繁 429 限流，
    分类 RSS 是每日更新的静态列表，稳定得多。只取 announce_type=new，按标题/摘要命中关键词。
    """
    categories = config.get('categories') or ['cs.RO']
    keywords = config.get('keywords') or DEFAULT_ARXIV_KEYWORDS
    max_results = config.get('max_results', 5)
    seen, papers = set(), []
    for category in categories:
        response = client.get(ARXIV_RSS + category, timeout=(10, 30))
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if not feed.entries and (feed.bozo or not feed.get('version')):
            raise ValueError('Invalid arXiv RSS response')
        for entry in feed.entries:
            try:
                if entry.get('arxiv_announce_type') not in (None, 'new'):
                    continue
                title = clean(entry.get('title'))
                summary = ARXIV_PREFIX.sub('', clean(entry.get('summary')))
                if not any(matches(k, title + ' ' + summary) for k in keywords):
                    continue
                url = canonical_url(entry.get('link', ''))
                if url in seen:
                    continue
                published = parse_date(entry.get('published_parsed')) or now
                seen.add(url)
                # 摘要保留前 240 字用于推送，完整内容点标题看原文。
                papers.append(Article(title, url, summary[:240], published, 'arXiv 论文', 0, '论文'))
            except (ValueError, TypeError, OverflowError):
                continue
    papers.sort(key=lambda article: article.published, reverse=True)
    return papers[:max_results]


def fetch_github(client, config, now):
    """GitHub 近期最受关注的新项目（按 star 数倒序）。"""
    import os
    days = config.get('days', 7)
    min_stars = config.get('min_stars', 100)
    max_results = config.get('max_results', 5)
    created = (now - timedelta(days=days)).strftime('%Y-%m-%d')
    headers = {'Accept': 'application/vnd.github+json'}
    if os.getenv('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    response = client.get('https://api.github.com/search/repositories', headers=headers, params={
        'q': f'created:>{created} stars:>={min_stars} archived:false',
        'sort': 'stars', 'order': 'desc', 'per_page': min(50, max_results)}, timeout=(10, 30))
    response.raise_for_status()
    data = response.json()
    repos = []
    for item in data.get('items', [])[:max_results]:
        try:
            summary = clean(item.get('description'))[:160]
            topics = (item.get('topics') or [])[:5]
            if topics:
                summary += ('  Topics: ' + ', '.join(topics))
            repos.append(Article(item['full_name'], canonical_url(item['html_url']), summary,
                                 parse_date(item.get('created_at')), 'GitHub 趋势项目',
                                 item.get('stargazers_count', 0), '趋势项目'))
        except (ValueError, TypeError, KeyError):
            continue
    return repos


def fetch_hackernews(client, config, now):
    """Hacker News 头部热帖 Top N。"""
    max_results = config.get('max_results', 5)
    top = client.get('https://hacker-news.firebaseio.com/v0/topstories.json', timeout=(10, 30))
    top.raise_for_status()
    stories = []
    for story_id in (top.json() or [])[:max_results]:
        response = client.get(f'https://hacker-news.firebaseio.com/v0/item/{story_id}.json',
                              timeout=(10, 30))
        response.raise_for_status()
        item = response.json() or {}
        title = item.get('title')
        if not title:
            continue
        url = item.get('url') or f'https://news.ycombinator.com/item?id={story_id}'
        published = (datetime.fromtimestamp(item['time'], timezone.utc)
                     if item.get('time') else now)
        try:
            stories.append(Article(title, canonical_url(url), '', published,
                                   'Hacker News', 0, '热帖'))
        except (ValueError, TypeError):
            continue
    return stories


def collect_extras(config, now):
    """依次抓取三类来源，返回 (Article 列表, 状态列表)。

    单个来源失败只记入 status（ok=False），其余照常；不使用线程，避免额外复杂度。
    """
    cfg = config.get('extras', {})
    articles, status = [], []
    fetchers = [('arXiv', fetch_arxiv, cfg.get('arxiv', {})),
                ('GitHub', fetch_github, cfg.get('github', {})),
                ('Hacker News', fetch_hackernews, cfg.get('hackernews', {}))]
    with session() as client:
        for name, fetch, options in fetchers:
            if not options.get('enabled', True):
                continue
            try:
                batch = fetch(client, options, now)
                articles.extend(batch)
                status.append({'source': name, 'ok': True, 'count': len(batch)})
                print(f'OK extras {name}: {len(batch)} entries')
            except Exception as exc:
                response = getattr(exc, 'response', None)
                code = response.status_code if response is not None else None
                status.append({'source': name, 'ok': False, 'error': type(exc).__name__, 'http_status': code})
                print(f'WARNING extras {name}: {type(exc).__name__}, HTTP {code}')
    return articles, status

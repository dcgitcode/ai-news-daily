"""每日「专业资讯速递」：中文 AI/科技资讯 + arXiv 具身智能论文（中文速读）。

选型约束：本模块运行在 GitHub Actions（境外机房），只使用免费、境外可达的公开数据源，
不依赖任何需管理员/建群/付费的通道。

中文化策略（2026-09-29 调整）：
- 中文源打底：量子位（AI 专业向）+ Solidot 奇客，纯中文、免翻译，命中率与摘要质量互补。
- arXiv 一手论文保留，但英文标题与摘要交给 llm.digest_zh 改写成中文速读；
  未配置 LLM_API_KEY 或调用失败时降级为「只发英文标题 + 链接」，不再整段抛英文摘要。
- GitHub 趋势 / Hacker News 已停用（英文生态，与「看得懂」冲突），代码保留备用。

产出统一转成 core.Article，复用 core.render 与 delivery 的渲染/推送逻辑。
任一数据源失败只记录状态、不影响其余来源，也不会让整个日报任务失败。
"""

import re
from datetime import datetime, timedelta, timezone

import feedparser

from . import llm
from .core import Article, canonical_url, chinese_readable, clean, matches, parse_date
from .sources import extract_image, session

# arXiv 分类 RSS 摘要前缀形如「arXiv:2509.xxxxx Announce Type: new\nAbstract: ...」，需剥掉。
ARXIV_PREFIX = re.compile(r'^arXiv:\S+\s+Announce Type:\s*\w+\s*Abstract:\s*', re.IGNORECASE)

# 具身智能 / 机器人方向关键词，对齐丁老师的教学与研究方向。
DEFAULT_ARXIV_KEYWORDS = ['embodied', 'vision-language-action', 'VLA', 'robot learning', 'humanoid',
                          'sim-to-real', 'sim2real', 'manipulation', 'legged', 'grasping',
                          'imitation learning', 'world model', 'diffusion policy']

# 中文源关键词。中文词用子串匹配（matches 对非 ASCII 不做词边界），够用且不会误伤。
DEFAULT_CHINESE_KEYWORDS = ['具身', '机器人', '人形', '机械臂', '抓取', '四足', '世界模型',
                            'VLA', '强化学习', '模仿学习', '自动驾驶', '多模态', '智能体',
                            '大模型', '人工智能', 'AI', '神经网络', '算力']

ARXIV_RSS = 'https://rss.arxiv.org/rss/'
# 摘要补足后仍不足这个长度，就认为正文首段没取到，退回 RSS 自带摘要。
LEAD_MIN_CHARS = 40


def fetch_arxiv(client, config, now):
    """arXiv 当日新论文（分类 RSS + 关键词过滤 + 中文速读）。

    用分类 RSS（rss.arxiv.org）而非 search API——API 对共享 IP 频繁 429 限流，
    分类 RSS 是每日更新的静态列表，稳定得多。只取 announce_type=new，按标题/摘要命中关键词。
    """
    categories = config.get('categories') or ['cs.RO']
    keywords = config.get('keywords') or DEFAULT_ARXIV_KEYWORDS
    max_results = config.get('max_results', 5)
    translate = config.get('translate', True)
    seen, raw = set(), []
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
                abstract = ARXIV_PREFIX.sub('', clean(entry.get('summary')))
                if not any(matches(k, title + ' ' + abstract) for k in keywords):
                    continue
                url = canonical_url(entry.get('link', ''))
                if url in seen:
                    continue
                seen.add(url)
                raw.append((title, abstract, url, parse_date(entry.get('published_parsed')) or now))
            except (ValueError, TypeError, OverflowError):
                continue
    raw.sort(key=lambda item: item[3], reverse=True)

    papers = []
    for title, abstract, url, published in raw[:max_results]:
        zh_title, zh_summary = ('', '')
        if translate:
            zh_title, zh_summary = llm.digest_zh(title, abstract)
        if zh_title:
            # 英文原标题留在 title、中文译文放 subtitle 另起一行：
            # 保留原文便于对照与检索，去重哈希也基于原文、跨天稳定。
            papers.append(Article(title, url, zh_summary, published, 'arXiv 论文', 0, '论文',
                                  subtitle=zh_title))
        elif translate:
            # 中文速读不可用：只给标题+链接。不放整段英文摘要——通篇英文正是要解决的问题。
            papers.append(Article(title, url, '', published, 'arXiv 论文', 0, '论文'))
        else:
            papers.append(Article(title, url, abstract[:240], published, 'arXiv 论文', 0, '论文'))
    return papers


def article_lead(html_text, title):
    """从中文资讯页 HTML 提取正文首段（best-effort）。

    仅在来源 RSS 只给一行导语时用于补足内容。选择器失效时返回空串，
    由调用方退回 RSS 摘要——补足失败绝不影响整条速递。
    """
    # 必须要求 </div> 紧接 article_info/tags——否则会停在前面的小 article 容器上（实测只取到 58 字的标题）。
    match = re.search(r'<div[^>]+class="[^"]*(?<![\w-])article(?![\w-])[^"]*"[^>]*>(.*?)</div>\s*'
                      r'<div[^>]+class="[^"]*(?:article_info|tags)[^"]*"', html_text, re.S)
    if not match:
        return ''
    body = clean(match.group(1))
    # 量子位页面在正文前有「作者 时间 来源：… 导语 … 公众号 QbitAI」等版式噪声，取标记之后。
    stripped = re.sub(r'^.*?公众号\s*\w+\s*', '', body, count=1)
    if stripped == body:
        stripped = body[len(title):] if body.startswith(title) else body
        stripped = re.sub(r'^\s*\S{1,16}\s+\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2}\s*', '', stripped)
        stripped = re.sub(r'^来源：\s*\S+\s*', '', stripped)
    return stripped.strip()


def _fetch_text(client, url):
    response = client.get(url, timeout=(10, 30))
    response.raise_for_status()
    return response.content.decode('utf-8', 'ignore')


def fetch_chinese(client, config, now):
    """中文 AI/科技资讯（国内媒体 RSS + 关键词过滤）。

    来源互补，任一来源内单条异常只跳过该条：
    - 量子位：AI 专业向、标题命中率高，但 RSS 只给一行导语 → enrich 抓正文首段补足。
    - Solidot 奇客：RSS 自带成段中文摘要，但覆盖面偏泛 → title_only 只按标题匹配，
      避免泛科技摘要里的偶然命中（实测「小偷想偷英伟达芯片」会因正文提到自动驾驶而误入）。
    """
    days = config.get('days', 3)
    keywords = config.get('keywords') or DEFAULT_CHINESE_KEYWORDS
    cutoff = now - timedelta(days=days)
    items, seen = [], set()
    for source in config.get('feeds') or []:
        response = client.get(source['url'], timeout=(10, 30))
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if not feed.entries and (feed.bozo or not feed.get('version')):
            raise ValueError('Invalid RSS response: ' + str(source.get('name')))
        quota = source.get('max_results', 3)
        matched = 0
        for entry in feed.entries:
            if matched >= quota:
                break
            try:
                title = clean(entry.get('title'))
                if not chinese_readable(title):
                    continue
                summary = clean(entry.get('summary') or
                                next((c.get('value') for c in entry.get('content') or []), ''))
                scope = title if source.get('title_only') else title + ' ' + summary[:300]
                if not any(matches(k, scope) for k in keywords):
                    continue
                url = canonical_url(entry.get('link', ''))
                if url in seen:
                    continue
                published = parse_date(entry.get('published_parsed')) or now
                if published < cutoff:
                    continue
                if source.get('enrich') and len(summary) < 80:
                    try:
                        lead = article_lead(_fetch_text(client, url), title)
                        if len(lead) >= LEAD_MIN_CHARS:
                            summary = lead
                    except Exception:
                        pass
                seen.add(url)
                matched += 1
                items.append(Article(title, url, summary[:220], published, source['name'],
                                     0, '资讯', image=extract_image(entry) or ''))
            except (ValueError, TypeError, OverflowError, AttributeError):
                continue
    items.sort(key=lambda article: article.published, reverse=True)
    return items[:config.get('max_results', 5)]


def fetch_github(client, config, now):
    """GitHub 近期最受关注的新项目（按 star 数倒序）。当前已停用，保留备用。"""
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
    """Hacker News 头部热帖 Top N。当前已停用，保留备用。"""
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
    """依次抓取各来源，返回 (Article 列表, 状态列表)。

    单个来源失败只记入 status（ok=False），其余照常；不使用线程，避免额外复杂度。
    """
    cfg = config.get('extras', {})
    articles, status = [], []
    if cfg.get('arxiv', {}).get('enabled') and not llm.enabled():
        print('WARNING extras arXiv: 未配置 LLM_API_KEY，论文只发英文标题（无中文速读）')
    llm.reset_stats()
    fetchers = [('arXiv', fetch_arxiv, cfg.get('arxiv', {})),
                ('中文源', fetch_chinese, cfg.get('chinese', {})),
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
                status.append({'source': name, 'ok': False, 'error': type(exc).__name__,
                               'http_status': code})
                print(f'WARNING extras {name}: {type(exc).__name__}, HTTP {code}')
    # 中文速读结果单独记一条：线上只看 status.json 也能判断大模型有没有真正跑通。
    if cfg.get('arxiv', {}).get('enabled') and cfg.get('arxiv', {}).get('translate', True):
        zh = llm.stats()
        status.append({'source': '中文速读', 'ok': zh['ok'] > 0,
                       'count': zh['ok'], 'failed': zh['fail'],
                       'reason': zh['reason'] or None})
        print(f'LLM 中文速读: 成功 {zh["ok"]} 篇, 失败 {zh["fail"]} 篇'
              + (f', 原因 {zh["reason"]}' if zh['reason'] else ''))
    return articles, status

import os
import re
from datetime import timedelta

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .core import Article, canonical_url, clean, parse_date

IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
IMAGE_EXT = re.compile(r'\.(jpe?g|png|webp|gif)([?#]|$)', re.IGNORECASE)


def extract_image(entry):
    """从 RSS 条目抽取配图：优先媒体字段，其次 enclosure，最后正文首个 <img>。

    媒体字段/enclosure 的地址即使无扩展名也接受（CDN 常不带后缀）；
    正文 <img> 必须带图片扩展名，避免把跳转链接当图。
    """
    media_urls = []
    for media in list(entry.get('media_content') or []) + list(entry.get('media_thumbnail') or []):
        url = media.get('url')
        if url and url.startswith(('http://', 'https://')):
            media_urls.append(url)
    for enclosure in entry.get('enclosures') or []:
        href = enclosure.get('href') or enclosure.get('url')
        if href and href.startswith(('http://', 'https://')):
            media_urls.append(href)
    for link in entry.get('links') or []:
        if link.get('rel') == 'enclosure' and str(link.get('type', '')).startswith('image'):
            href = link.get('href')
            if href and href.startswith(('http://', 'https://')):
                media_urls.append(href)
    if media_urls:
        return media_urls[0]

    html_body = str(entry.get('summary') or '')
    for content in entry.get('content') or []:
        html_body += str(content.get('value') or '')
    for url in IMG_SRC.findall(html_body):
        if url.startswith(('http://', 'https://')) and IMAGE_EXT.search(url):
            return url
    return ''


def session():
    client = requests.Session()
    client.headers['User-Agent'] = 'AI-News-Daily/1.0 (personal RSS reader)'
    # Retry reads only; never automatically repeat push requests.
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=['GET'], respect_retry_after_header=False)
    client.mount('https://', HTTPAdapter(max_retries=retry))
    return client


def fetch_feed(client, url, name, weight, kind='新闻', params=None):
    response = client.get(url, params=params, timeout=(10, 30))
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if not feed.entries and (feed.bozo or not feed.get('version')):
        raise ValueError('Invalid RSS/Atom response')
    articles = []
    for entry in feed.entries:
        try:
            # Undated entries are excluded rather than labeled as today's news.
            date = parse_date(entry.get('published_parsed') or entry.get('updated_parsed'))
            article = Article(clean(entry.get('title')), canonical_url(entry.get('link', '')),
                              clean(entry.get('summary', '')), date, name, weight, kind,
                              image=extract_image(entry))
            if article.title:
                articles.append(article)
        except (ValueError, TypeError, OverflowError):
            continue
    return articles


def collect(config, now):
    articles, status = [], []
    with session() as client:
        jobs = [(s['name'], lambda s=s: fetch_feed(client, s['url'], s['name'], s['weight']))
                for s in config['rss']]
        arxiv = config['arxiv']
        if arxiv['enabled']:
            jobs.append(('arXiv', lambda: fetch_feed(client, 'https://export.arxiv.org/api/query',
                'arXiv', arxiv['weight'], '论文', {
                    'search_query': arxiv['query'], 'start': 0,
                    'max_results': min(100, arxiv['max_results']),
                    'sortBy': 'submittedDate', 'sortOrder': 'descending'})))
        github = config['github']
        if github['enabled']:
            def fetch_github():
                since = (now - timedelta(hours=config['lookback_hours'])).strftime('%Y-%m-%dT%H:%M:%SZ')
                headers = {'Accept': 'application/vnd.github+json'}
                if os.getenv('GITHUB_TOKEN'):
                    headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
                response = client.get('https://api.github.com/search/repositories', headers=headers,
                    params={'q': github['query'] + ' pushed:>=' + since, 'sort': 'stars',
                            'order': 'desc', 'per_page': min(100, github['max_results'])}, timeout=(10, 30))
                response.raise_for_status()
                data = response.json()
                if data.get('incomplete_results'):
                    print('WARNING GitHub search returned incomplete results')
                return [Article(r['full_name'], canonical_url(r['html_url']),
                                clean(r.get('description')) + ' Topics: ' + ', '.join(r.get('topics', [])),
                                parse_date(r['pushed_at']), 'GitHub', github['weight'],
                                '近期更新项目', r['stargazers_count']) for r in data['items']]
            jobs.append(('GitHub', fetch_github))
        for name, fetch in jobs:
            try:
                batch = fetch()
                articles.extend(batch)
                status.append({'source': name, 'ok': True, 'count': len(batch)})
                print(f'OK {name}: {len(batch)} entries')
            except Exception as exc:
                # Exception messages can contain URLs/tokens; only log type and HTTP status.
                response = getattr(exc, 'response', None)
                code = response.status_code if response is not None else None
                status.append({'source': name, 'ok': False, 'error': type(exc).__name__, 'http_status': code})
                print(f'WARNING {name}: {type(exc).__name__}, HTTP {code}')
    if not status or not any(s['ok'] for s in status):
        raise RuntimeError('All sources failed or no sources enabled')
    return articles, status

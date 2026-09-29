"""微信公众号草稿箱推送。

**为什么只到草稿箱、不做发布**
2025 年 7 月起，微信回收了个人主体账号（及企业未认证账号）的 freepublish/submit
（发布）接口权限，而个人订阅号没有微信认证入口——这是个死循环，不是技术问题。
所以 API 最多把文章推进草稿箱，最后一步「发布」须在公众号后台或「订阅号助手」App 手动点。

**为什么必须本地跑、不能接进 daily.yml**
微信要求调用方 IP 预先进「IP 白名单」，GitHub Actions 出口 IP 是动态的（微信又不支持
IP 段/通配符），从 Actions 调会直接报 40164 invalid ip。故本模块只由本地入口调用。

**正文形态：只放标题 + 摘要 + 来源，不放正文图**
- 外部图片链接会被微信拦截（「此图片来自微信公众平台未经允许不可引用」），
  要让图片显示必须先上传到微信素材库并替换链接，成本高、收益低。
- 日报本就大量摘录他人内容，整段搬正文有转载与原创校验风险，做成「导读」更稳。

**凭证**：WECHAT_APPID / WECHAT_SECRET，放本地 .env（不进仓库）。
"""

import hashlib
import html
import json
import os
import time
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

API = 'https://api.weixin.qq.com/cgi-bin'
# 微信正文上限 2 万字符、1MB，留出余量提前收口。
MAX_CONTENT_CHARS = 19000
MAX_TITLE_BYTES = 60      # 微信限 64 字节，留 4 字节余量
MAX_DIGEST_CHARS = 120    # 微信限 128 字
# 单条摘要上限。日报主条目本来就是短摘录（≤280 字），这里主要收的是中文媒体的长正文首段
# （量子位实测可取到 2800+ 字）——不设限的话 20 条能拼到 5.6 万字符，直接超微信上限。
# 800 字够读者在微信里读完一条要闻，又给 20 条留足余量。
SUMMARY_CHARS = 800


def _beijing(moment):
    return moment.astimezone(timezone(timedelta(hours=8)))


def credentials():
    appid = os.getenv('WECHAT_APPID', '').strip()
    secret = os.getenv('WECHAT_SECRET', '').strip()
    if not appid or not secret:
        raise RuntimeError('missing WECHAT_APPID / WECHAT_SECRET')
    return appid, secret


def _json(response, what):
    """微信接口响应统一收口：errcode 非 0 一律抛错。

    只带 errcode/errmsg —— access_token 出现在请求 URL 上，
    任何从 response 派生的文本都不得进入异常信息。
    """
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f'{what}: invalid response') from None
    code = data.get('errcode', 0)
    if code:
        raise RuntimeError(f'{what}: errcode {code} {data.get("errmsg", "")}'.strip())
    return data


def _post(url, what, **kwargs):
    try:
        return requests.post(url, **kwargs)
    except requests.RequestException as exc:
        # requests 的异常文本会带上含 access_token 的 URL，只保留类型。
        raise RuntimeError(f'{what}: {type(exc).__name__}') from None


def access_token(force=False):
    """拿 access_token，带文件缓存（微信有效期 7200 秒，且接口有调用频率限制）。"""
    appid, secret = credentials()
    cache_path = Path(os.getenv('WECHAT_TOKEN_CACHE', 'state/wechat_token.json'))
    now = time.time()
    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            # 提前 5 分钟作废，避免踩到过期边界。
            if cached.get('appid') == appid and cached.get('expires_at', 0) > now + 300:
                return cached['token']
        except (ValueError, OSError):
            pass
    try:
        response = requests.get(f'{API}/token', timeout=(10, 30), params={
            'grant_type': 'client_credential', 'appid': appid, 'secret': secret})
    except requests.RequestException as exc:
        raise RuntimeError(f'wechat token: {type(exc).__name__}') from None
    data = _json(response, 'wechat token')
    token = data.get('access_token')
    if not token:
        raise RuntimeError('wechat token: empty access_token')
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        'appid': appid, 'token': token,
        'expires_at': now + int(data.get('expires_in', 7200)) - 300}), encoding='utf-8')
    return token


def upload_image(image_bytes, filename='cover.jpg'):
    """把图片作为永久素材上传，返回 media_id（草稿封面必须是永久素材）。"""
    response = _post(f'{API}/material/add_material', 'wechat material',
                     params={'access_token': access_token(), 'type': 'image'},
                     files={'media': (filename, image_bytes)}, timeout=(10, 60))
    media_id = _json(response, 'wechat material').get('media_id')
    if not media_id:
        raise RuntimeError('wechat material: empty media_id')
    return media_id


def cover_media_id(articles):
    """挑一张封面：优先当日首篇带配图的新闻，回落到 WECHAT_COVER 指定的本地图。

    按图片内容的 md5 缓存 media_id —— 图片没变就不重复上传，
    免得每次运行都在素材库里堆一张新图。
    """
    image, name = None, 'cover.jpg'
    for article in articles:
        url = article.image
        if not (url and url.startswith('https://')):
            continue
        try:
            fetched = requests.get(url, timeout=(10, 30),
                                   headers={'User-Agent': 'Mozilla/5.0'})
            fetched.raise_for_status()
            if fetched.content:
                image = fetched.content
                suffix = Path(urlparse(url).path).suffix or '.jpg'
                name = 'cover' + (suffix if suffix.lower() in ('.jpg', '.jpeg', '.png') else '.jpg')
                break
        except requests.RequestException:
            continue
    if image is None:
        fixed = os.getenv('WECHAT_COVER', '').strip()
        if fixed:
            try:
                image = Path(fixed).read_bytes()
                name = Path(fixed).name
            except OSError:
                image = None
    if image is None:
        raise RuntimeError('no cover: 当日新闻无配图，请用 WECHAT_COVER 指定一张本地封面图')

    cache_path = Path(os.getenv('WECHAT_COVER_CACHE', 'state/wechat_cover.json'))
    key = hashlib.md5(image).hexdigest()
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            cache = {}
    if cache.get('key') == key and cache.get('media_id'):
        return cache['media_id']
    media_id = upload_image(image, name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({'key': key, 'media_id': media_id}), encoding='utf-8')
    return media_id


def entry_html(index, article, summary_chars=SUMMARY_CHARS):
    """单条：标题 / 中文译题（如有）/ 摘要 / 来源日期。全部内联样式。

    正文不放链接 —— 公众号正文里的外链 <a> 会被微信过滤掉，
    放了也是纯文本，不如把出口留给「阅读原文」。
    """
    esc = html.escape
    parts = ['<section style="margin:0 0 20px">',
             f'<p style="font-size:17px;line-height:1.55;font-weight:bold;color:#1f1f1f;'
             f'margin:0 0 6px">{index}. {esc(article.title)}</p>']
    if article.subtitle:
        parts.append(f'<p style="font-size:15px;line-height:1.6;color:#4a6fa5;'
                     f'margin:0 0 6px">{esc(article.subtitle)}</p>')
    if article.summary:
        parts.append(f'<p style="font-size:15px;line-height:1.8;color:#3f3f3f;'
                     f'margin:0 0 8px">{esc(article.summary[:summary_chars])}</p>')
    parts.append(f'<p style="font-size:12px;line-height:1.6;color:#9aa0a6;margin:0">'
                 f'{esc(article.source)} · {_beijing(article.published):%Y-%m-%d}</p>')
    parts.append('</section>')
    return ''.join(parts)


def section_html(heading, articles, summary_chars=SUMMARY_CHARS):
    esc = html.escape
    parts = [f'<p style="font-size:18px;font-weight:bold;color:#12325c;line-height:1.6;'
             f'border-left:4px solid #12325c;padding-left:10px;margin:26px 0 14px">{esc(heading)}</p>']
    parts.extend(entry_html(i, article, summary_chars) for i, article in enumerate(articles, 1))
    return ''.join(parts)


def _compose(sections, note, footer, summary_chars=SUMMARY_CHARS):
    esc = html.escape
    parts = [f'<p style="font-size:13px;line-height:1.7;color:#9aa0a6;margin:0 0 4px">{esc(note)}</p>']
    for heading, articles in sections:
        if articles:
            parts.append(section_html(heading, articles, summary_chars))
    if footer:
        parts.append(f'<p style="font-size:13px;line-height:1.7;color:#9aa0a6;'
                     f'margin:26px 0 0">{esc(footer)}</p>')
    return ''.join(parts)


def render_article(sections, note, footer='', summary_chars=SUMMARY_CHARS,
                   max_chars=MAX_CONTENT_CHARS):
    """拼公众号正文。sections: [(小节标题, [Article, ...]), ...]。

    超出上限时从**最后一节末尾**开始丢条目（速递是补充内容，先牺牲），丢了几条会打印出来。
    静默截断比报错更难排查 —— 宁可日志里留一行。
    """
    kept = [(heading, list(articles)) for heading, articles in sections]
    dropped = 0
    while True:
        content = _compose(kept, note, footer, summary_chars)
        if len(content) <= max_chars:
            break
        for _, articles in reversed(kept):
            if articles:
                articles.pop()
                dropped += 1
                break
        else:
            raise RuntimeError(
                f'wechat content too long even after dropping all entries: {len(content)} chars')
    if dropped:
        print(f'WARNING wechat: 正文超出 {max_chars} 字符，已从末尾丢弃 {dropped} 条')
    return content


def add_draft(title, content, thumb_media_id, digest='', source_url='', author=''):
    """新建图文草稿，返回草稿 media_id。"""
    article = {'article_type': 'news', 'title': _clip_bytes(title, MAX_TITLE_BYTES),
               'thumb_media_id': thumb_media_id, 'content': content,
               'need_open_comment': 0, 'only_fans_can_comment': 0}
    if digest:
        article['digest'] = digest[:MAX_DIGEST_CHARS]
    if source_url:
        article['content_source_url'] = source_url
    if author:
        article['author'] = author
    payload = json.dumps({'articles': [article]}, ensure_ascii=False).encode('utf-8')
    response = _post(f'{API}/draft/add', 'wechat draft',
                     params={'access_token': access_token()}, data=payload,
                     headers={'Content-Type': 'application/json'}, timeout=(10, 30))
    return _json(response, 'wechat draft').get('media_id', '')


def _clip_bytes(text, limit):
    """按 UTF-8 字节截断，避免把一个汉字劈成半个。"""
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    return raw[:limit].decode('utf-8', 'ignore')


def push_draft(main_articles, extra_articles, date_text):
    """把主日报 + 专业资讯速递合成一篇草稿推进公众号。返回 (draft_media_id, title)。"""
    title = f'AI 日报 | {date_text}'
    sections = [('今日要闻', main_articles), ('专业资讯速递', extra_articles)]
    content = render_article(
        sections,
        note='本文为公开资讯摘录，版权归原作者所有；点击文末「阅读原文」查看完整版。',
        footer='内容由 AI 汇总整理，仅作信息参考。')
    head = main_articles[0].title if main_articles else '今日 AI 要闻'
    digest = f'{head}｜今日要闻 {len(main_articles)} 条，专业资讯速递 {len(extra_articles)} 条。'
    source_url = os.getenv('WECHAT_SOURCE_URL', '').strip()
    media_id = add_draft(title, content, cover_media_id(main_articles + extra_articles),
                         digest=digest, source_url=source_url,
                         author=os.getenv('WECHAT_AUTHOR', '').strip())
    return media_id, title

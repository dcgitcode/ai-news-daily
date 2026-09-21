import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage

import requests

from .core import Article, canonical_url


def required(key):
    value = os.getenv(key, '').strip()
    if not value:
        raise ValueError(f'Missing setting: {key}')
    return value


def wecom_markdown(title, plain):
    """把纯文本日报转成企业微信群机器人 markdown（上限 4096 字节）。

    纯文本结构由 core.render 生成：每个条目依次为「N. 标题 / 元信息 / 摘要 / 链接」。
    超长时先砍摘要长度，再砍条目数，保证消息能发出去。
    """
    items, current = [], None
    for line in plain.split('\n'):
        numbered = re.match(r'^(\d+)\. (.+)$', line)
        if numbered:
            current = {'no': numbered.group(1), 'title': numbered.group(2),
                       'source': '', 'summary': '', 'url': ''}
            items.append(current)
        elif current is None:
            continue
        elif line.startswith('http'):
            current['url'] = line
        elif ' · ' in line and '分数' in line:
            current['source'] = line.split(' · ')[1] if ' · ' in line else ''
        elif line and not current['summary']:
            current['summary'] = line

    header = f'## {title}\n'
    footer = '\n> 完整图文版见邮件'

    def compose(summary_chars):
        parts = [header]
        for item in items:
            line = f'**{item["no"]}. [{item["title"]}]({item["url"]})**' \
                   f'<font color="comment"> {item["source"]}</font>\n'
            if summary_chars and item['summary']:
                line += item['summary'][:summary_chars] + '\n'
            parts.append(line + '\n')
        return ''.join(parts) + footer

    for limit in (120, 60, 0):
        content = compose(limit)
        if len(content.encode('utf-8')) <= 4000:
            return content
    # 仍超长则删减条目，从尾部丢弃。
    while len(items) > 3 and len(compose(0).encode('utf-8')) > 4000:
        items.pop()
    return compose(0)


def _truncate_bytes(text, limit):
    """按字节安全截断，避免切断半个中文；超限尾部加省略号。"""
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text
    return data[:limit - 3].decode('utf-8', 'ignore') + '…'


def get_wecom_token(corpid, corpsecret):
    """企业微信自建应用：用 corpid + corpsecret 换取 access_token（有效期 7200s）。"""
    response = requests.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',
                            params={'corpid': corpid, 'corpsecret': corpsecret}, timeout=(10, 30))
    response.raise_for_status()
    data = response.json()
    if data.get('errcode') != 0:
        raise RuntimeError(f'WeCom token rejected (errcode {data.get("errcode")})')
    token = data.get('access_token')
    if not token:
        raise RuntimeError('WeCom token missing in response')
    return token


def wecom_app_cards(articles):
    """把 Article 列表转成企业微信自建应用 news 图文卡片（含 RSS 配图）。

    每条一个卡片，单条消息最多 8 条；超出自动分批。图片仅使用 https 来源，
    与 render() 的配图策略一致。标题/摘要按字节上限截断防止接口拒绝。
    """
    chunks = [articles[i:i + 8] for i in range(0, len(articles), 8)]
    messages = []
    for chunk in chunks:
        arts = []
        for article in chunk:
            art = {'title': _truncate_bytes(article.title, 120),
                   'description': _truncate_bytes(article.summary, 480),
                   'url': canonical_url(article.url)}
            if article.image and article.image.startswith('https://'):
                art['picurl'] = article.image[:1024]
            arts.append(art)
        messages.append({'msgtype': 'news', 'news': {'articles': arts}})
    return messages


def pushplus_html(title, articles):
    """把图文日报渲染成 PushPlus 的 HTML 模板内容（个人微信消息）。

    每条含标题链接、来源、摘要与 RSS 配图（仅 https，否则省略图片标签），
    图片用 <img> 内联，符合 PushPlus 文档要求（图片地址须 https）。
    无配图或被防盗链时仍能正常显示文字，属于降级而非失败。
    """
    esc = html.escape
    blocks = [f'<h2>{esc(title)}</h2><hr>']
    for article in articles:
        url = canonical_url(article.url)
        meta = f'{esc(article.source)} · {article.published:%Y-%m-%d}'
        block = ['<div>']
        block.append(f'<a href="{esc(url, quote=True)}"><b>{esc(article.title)}</b></a><br>')
        block.append(f'<font color="gray">{meta}</font><br>')
        if article.summary:
            block.append(f'<p>{esc(article.summary)}</p>')
        if article.image and article.image.startswith('https://'):
            block.append(f'<img src="{esc(article.image, quote=True)}" style="max-width:100%"><br>')
        block.append('</div><hr>')
        blocks.append(''.join(block))
    blocks.append('<p><font color="gray">完整图文版见邮件</font></p>')
    return ''.join(blocks)


def validate_channels(channels):
    if not channels:
        raise ValueError('Set CHANNELS=email,pushplus or use --dry-run')
    for channel in channels:
        if channel == 'email':
            for key in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'EMAIL_TO'):
                required(key)
            if os.getenv('SMTP_SECURITY', 'ssl') not in ('ssl', 'starttls'):
                raise ValueError('SMTP_SECURITY must be ssl or starttls')
            if not 1 <= int(os.getenv('SMTP_PORT', '465')) <= 65535:
                raise ValueError('Invalid SMTP_PORT')
        elif channel == 'pushplus':
            required('PUSHPLUS_TOKEN')
        elif channel == 'wecombot':
            required('WECOM_WEBHOOK')
        elif channel == 'wecom_app':
            for key in ('WECOM_CORPID', 'WECOM_CORPSECRET', 'WECOM_AGENTID', 'WECOM_TOUSER'):
                required(key)
        else:
            raise ValueError('Unsupported channel: ' + channel)


def send(channel, title, page, plain, articles=None):
    if channel == 'email':
        message = EmailMessage()
        message['Subject'] = title
        message['From'] = os.getenv('EMAIL_FROM') or required('SMTP_USER')
        message['To'] = required('EMAIL_TO')
        message.set_content(plain)
        message.add_alternative(page, subtype='html')
        host, port = required('SMTP_HOST'), int(os.getenv('SMTP_PORT', '465'))
        context = ssl.create_default_context()
        if os.getenv('SMTP_SECURITY', 'ssl') == 'ssl':
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            if os.getenv('SMTP_SECURITY', 'ssl') == 'starttls':
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(required('SMTP_USER'), required('SMTP_PASSWORD'))
            refused = server.send_message(message)
            if refused:
                raise RuntimeError('One or more email recipients were refused')
        return 'smtp_accepted'
    if channel == 'pushplus':
        token = required('PUSHPLUS_TOKEN')
        # 有结构化图文时走 HTML 模板（带 RSS 配图）；否则退回纯文本，保证能用。
        if articles:
            content, template = pushplus_html(title, articles), 'html'
        else:
            content, template = plain, 'txt'
        response = requests.post('https://www.pushplus.plus/send', json={
            'token': token, 'title': title,
            'content': content, 'template': template, 'channel': 'wechat'}, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if data.get('code') != 200:
            raise RuntimeError('Pushplus rejected request')
        # 200 means queued, not delivered. Store receipt for provider-side diagnosis.
        return 'pushplus_queued:' + str(data.get('data', ''))
    if channel == 'wecombot':
        webhook = required('WECOM_WEBHOOK')
        receipts = []
        # 优先用图文卡片（含 RSS 配图，与自建应用同构）；无结构化文章时退回 markdown。
        if articles:
            for payload in wecom_app_cards(articles):
                response = requests.post(webhook, json=payload, timeout=(10, 30))
                response.raise_for_status()
                data = response.json()
                if data.get('errcode') != 0:
                    raise RuntimeError(f'WecomBot rejected request (errcode {data.get("errcode")})')
                receipts.append(str(data.get('msgid', 'ok')))
            return 'wecombot_accepted:' + ','.join(receipts)
        content = wecom_markdown(title, plain)
        response = requests.post(webhook, json={
            'msgtype': 'markdown', 'markdown': {'content': content}}, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        # 企业微信成功返回 {"errcode":0}；非 0 一律视为失败。
        if data.get('errcode') != 0:
            raise RuntimeError(f'WecomBot rejected request (errcode {data.get("errcode")})')
        return 'wecombot_accepted'
    if channel == 'wecom_app':
        if not articles:
            raise ValueError('wecom_app requires structured articles')
        corpid = required('WECOM_CORPID')
        corpsecret = required('WECOM_CORPSECRET')
        agentid = int(required('WECOM_AGENTID'))
        touser = required('WECOM_TOUSER')
        token = get_wecom_token(corpid, corpsecret)
        receipts = []
        for payload in wecom_app_cards(articles):
            payload.update({'touser': touser, 'agentid': agentid})
            response = requests.post('https://qyapi.weixin.qq.com/cgi-bin/message/send',
                                     params={'access_token': token}, json=payload, timeout=(10, 30))
            response.raise_for_status()
            data = response.json()
            if data.get('errcode') != 0:
                raise RuntimeError(f'WeCom app send rejected (errcode {data.get("errcode")})')
            receipts.append(str(data.get('msgid', 'ok')))
        return 'wecom_app_accepted:' + ','.join(receipts)
    raise ValueError('Unsupported channel')

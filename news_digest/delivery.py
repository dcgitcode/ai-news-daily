import os
import re
import smtplib
import ssl
from email.message import EmailMessage

import requests


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
        else:
            raise ValueError('Unsupported channel: ' + channel)


def send(channel, title, page, plain):
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
        response = requests.post('https://www.pushplus.plus/send', json={
            'token': required('PUSHPLUS_TOKEN'), 'title': title,
            'content': plain, 'template': 'txt', 'channel': 'wechat'}, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if data.get('code') != 200:
            raise RuntimeError('Pushplus rejected request')
        # 200 means queued, not delivered. Store receipt for provider-side diagnosis.
        return 'pushplus_queued:' + str(data.get('data', ''))
    if channel == 'wecombot':
        content = wecom_markdown(title, plain)
        response = requests.post(required('WECOM_WEBHOOK'), json={
            'msgtype': 'markdown', 'markdown': {'content': content}}, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        # 企业微信成功返回 {"errcode":0}；非 0 一律视为失败。
        if data.get('errcode') != 0:
            raise RuntimeError(f'WecomBot rejected request (errcode {data.get("errcode")})')
        return 'wecombot_accepted'
    raise ValueError('Unsupported channel')

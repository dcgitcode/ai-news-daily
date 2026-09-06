import os
import smtplib
import ssl
from email.message import EmailMessage

import requests


def required(key):
    value = os.getenv(key, '').strip()
    if not value:
        raise ValueError(f'Missing setting: {key}')
    return value


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
    raise ValueError('Unsupported channel')

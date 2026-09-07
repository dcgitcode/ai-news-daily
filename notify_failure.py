"""Send a short failure alert through the configured SMTP account.

Used by GitHub Actions on job failure. Reads SMTP settings from the
environment (same names as the digest) plus ALERT_SUBJECT / ALERT_BODY.
Missing credentials are reported but never fail the job, so the alert step
cannot mask the original failure.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def build_message() -> EmailMessage:
    sender = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER", "")
    recipients = [
        address.strip()
        for address in os.getenv("EMAIL_TO", "").split(",")
        if address.strip()
    ]
    subject = os.getenv("ALERT_SUBJECT", "ai-news-daily 运行失败")
    body = os.getenv("ALERT_BODY", "")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body or subject)
    return message


def main() -> int:
    host = os.getenv("SMTP_HOST", "smtp.163.com").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    recipients = [
        address.strip()
        for address in os.getenv("EMAIL_TO", "").split(",")
        if address.strip()
    ]

    if not host or not user or not password or not recipients:
        print("告警未发送：SMTP 或收件人配置缺失，请检查仓库 Secrets。")
        return 0

    try:
        port = int(os.getenv("SMTP_PORT", "465"))
    except ValueError:
        print("告警未发送：SMTP_PORT 不是数字。")
        return 0

    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    message = build_message()

    try:
        if security == "starttls":
            with smtplib.SMTP(host, port, timeout=30) as client:
                client.starttls(context=ssl.create_default_context())
                client.login(user, password)
                client.send_message(message)
        else:
            with smtplib.SMTP_SSL(
                host, port, timeout=30, context=ssl.create_default_context()
            ) as client:
                client.login(user, password)
                client.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        print(f"告警发送失败：{error}")
        return 0

    print("告警邮件已发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

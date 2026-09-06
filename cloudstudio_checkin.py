"""Claim the Cloud Studio daily sign-in reward using a stored browser session."""

from __future__ import annotations

import os
import sys
from typing import Any

import requests


BASE_URL = "https://cloudstudio.net/api"
TASK_ID = "SIGN_IN_2025Q3"


def cookie_value(cookie_header: str, name: str) -> str | None:
    """Return one cookie value from a standard Cookie request header."""
    for item in cookie_header.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            return value
    return None


def xsrf_token(session_value: str) -> str:
    """Match the X-XSRF-TOKEN calculation used by Cloud Studio's web client."""
    value = 5381
    for char in session_value:
        value += (value << 5) + ord(char)
    return str(value & 2147483647)


def response_json(response: requests.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise RuntimeError("登录状态已失效：Cloud Studio 未返回 JSON 数据。请更新 CLOUDSTUDIO_COOKIE。")
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("Cloud Studio 返回的数据无法读取。") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Cloud Studio 返回了意外的数据格式。")
    return payload


def task_status(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    records = data.get("records")
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return None
    status = records[0].get("status")
    return str(status) if status is not None else None


def claim_daily_reward(cookie_header: str, session: requests.Session | None = None) -> str:
    source_session = cookie_value(cookie_header, "skey") or cookie_value(cookie_header, "cloudstudio-session")
    if not source_session:
        raise ValueError("CLOUDSTUDIO_COOKIE 中缺少 skey 或 cloudstudio-session。")

    client = session or requests.Session()
    client.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": xsrf_token(source_session),
            "Cookie": cookie_header,
        }
    )

    detail_url = f"{BASE_URL}/billing/activityTask/{TASK_ID}"
    detail = response_json(client.get(detail_url, params={"lastRecord": "true"}, timeout=30))
    if detail.get("code") != 0:
        raise RuntimeError(str(detail.get("message") or detail.get("msg") or "读取签到状态失败。"))

    current_status = task_status(detail)
    if current_status in {"REWARDED", "REWARDING"}:
        return "今日已签到，无需重复领取。"

    reward = response_json(client.post(f"{detail_url}/_reward", timeout=30))
    if reward.get("code") != 0:
        raise RuntimeError(str(reward.get("message") or reward.get("msg") or "签到领取失败。"))

    status = task_status(reward)
    if status in {"REWARDED", "REWARDING"}:
        return "Cloud Studio 每日签到已完成。"
    raise RuntimeError(f"签到接口未确认成功，当前状态：{status or '未知'}。")


def main() -> int:
    cookie_header = os.getenv("CLOUDSTUDIO_COOKIE", "").strip()
    if not cookie_header:
        print("CLOUDSTUDIO_COOKIE 未配置。", file=sys.stderr)
        return 2
    try:
        print(claim_daily_reward(cookie_header))
        return 0
    except (ValueError, RuntimeError, requests.RequestException) as error:
        print(f"Cloud Studio 签到失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

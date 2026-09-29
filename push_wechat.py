#!/usr/bin/env python
"""把当日 AI 日报 + 专业资讯速递推成公众号草稿。**本地专用，不要接进 daily.yml。**

为什么单独一个脚本、不并进 `python -m news_digest`
1. 微信要求调用方 IP 在「IP 白名单」里，GitHub Actions 出口 IP 是动态的，从云端调必报 40164。
   放进 __main__.py 会让云端链路多一条注定失败的代码路径。
2. 它需要本地 .env 里的 WECHAT_APPID / WECHAT_SECRET，这两个值绝不能进 GitHub Secrets。
3. 发布是主动动作，不该被定时任务顺带触发。

用法
    python push_wechat.py              采集 → 渲染 → 推进草稿箱
    python push_wechat.py --dry-run    只渲染，落盘 reports/wechat-draft.html，不碰微信接口
    python push_wechat.py --force      当日已推过也重推（改排版后想看效果时用）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from news_digest.core import rank
from news_digest.extras import collect_extras
from news_digest.sources import collect
from news_digest.wechat import push_draft, render_article

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state' / 'wechat_draft.json'


def _already_pushed(today):
    if not STATE.exists():
        return False
    try:
        return json.loads(STATE.read_text(encoding='utf-8')).get('date') == today
    except (ValueError, OSError):
        return False


def _remember(today, media_id):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({'date': today, 'draft_media_id': media_id},
                                ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description='推送公众号草稿（本地运行）')
    parser.add_argument('--dry-run', action='store_true', help='只渲染，不调用微信接口')
    parser.add_argument('--force', action='store_true', help='当日已推过也重推')
    args = parser.parse_args()

    config = json.loads((ROOT / os.getenv('CONFIG_PATH', 'config.json')).read_text(encoding='utf-8'))
    now = datetime.now(timezone.utc)
    beijing = now.astimezone(timezone(timedelta(hours=8)))
    today, date_text = beijing.strftime('%Y-%m-%d'), beijing.strftime('%Y年%m月%d日')

    if not args.dry_run and not args.force and _already_pushed(today):
        print(f'今天（{today}）已经推过草稿，跳过。要重推加 --force。')
        return 0

    print('采集主日报…')
    articles, status = collect(config, now)
    ranked = rank(articles, config, now)[:config['max_items']]
    print(f'  主日报 {len(ranked)} 条（候选 {len(ranked)}）')
    for row in status:
        if not row.get('ok'):
            print(f'  WARNING 源 {row.get("source")} 失败: {row.get("error")}')

    extras = []
    if config.get('extras', {}).get('enabled'):
        print('采集专业资讯速递…')
        try:
            extras, extra_status = collect_extras(config, now)
            print(f'  速递 {len(extras)} 条')
            for row in extra_status:
                flag = 'OK ' if row.get('ok') else 'WARN'
                print(f'  {flag} {row.get("source")}: {row.get("count", 0)} 条'
                      + (f' 原因 {row.get("reason")}' if row.get('reason') else ''))
        except Exception as exc:
            # 速递失败不该拦住主日报草稿。
            print(f'  WARNING 速递采集失败: {type(exc).__name__}，本次只推主日报')

    if not ranked and not extras:
        print('没有任何条目，放弃推送。')
        return 1

    if args.dry_run:
        content = render_article(
            [('今日要闻', ranked), ('专业资讯速递', extras)],
            note='本文为公开资讯摘录，版权归原作者所有；点击文末「阅读原文」查看完整版。',
            footer='内容由 AI 汇总整理，仅作信息参考。')
        report_dir = ROOT / os.getenv('REPORT_DIR', 'reports')
        report_dir.mkdir(parents=True, exist_ok=True)
        out = report_dir / 'wechat-draft.html'
        # 包一层最小 HTML 壳，浏览器能直接看正文排版（公众号正文本身只认内联样式）。
        out.write_text(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>AI 日报 {date_text}</title>'
            '<body style="max-width:680px;margin:0 auto;padding:22px 18px;'
            'font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif">'
            f'<h1 style="font-size:22px">{date_text} AI 日报 · 公众号草稿预览</h1>'
            + content + '</body></html>', encoding='utf-8')
        print(f'DRY-RUN 已渲染：{out}（{len(content)} 字符 / 上限 19000）')
        return 0

    media_id, title = push_draft(ranked, extras, date_text)
    _remember(today, media_id)
    print(f'ACCEPTED wechat draft: 《{title}》media_id={media_id}')
    print('去公众号后台「草稿箱」检查排版，确认无误再点发布。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # 微信凭证会出现在请求 URL 上（access_token），只打类型不打 details。
        print(f'FATAL {type(exc).__name__}: {exc if isinstance(exc, RuntimeError) else "(详见说明)"}',
              file=sys.stderr)
        sys.exit(1)

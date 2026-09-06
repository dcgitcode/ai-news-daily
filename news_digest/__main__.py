import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from .core import Article, load_state, mark, pending, rank, render, save_state
from .delivery import send, validate_channels
from .sources import collect


def main():
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description='AI news digest without paid model APIs')
    parser.add_argument('--dry-run', action='store_true', help='Fetch and render without delivery/state mutation')
    parser.add_argument('--demo', action='store_true', help='Offline sample; implies dry-run')
    args = parser.parse_args()
    dry = args.dry_run or args.demo
    config = json.loads(Path(os.getenv('CONFIG_PATH', 'config.json')).read_text(encoding='utf-8'))
    if not 0 < config['lookback_hours'] <= 24 * 30 or not 1 <= config['max_items'] <= 50:
        raise ValueError('lookback_hours must be 1..720; max_items must be 1..50')
    if not 0.5 <= config['title_similarity'] <= 1 or not 1 <= config['summary_chars'] <= 1000:
        raise ValueError('Invalid title_similarity or summary_chars')
    channels = list(dict.fromkeys(c.strip() for c in os.getenv('CHANNELS', '').split(',') if c.strip()))
    if not dry:
        validate_channels(channels)
    state_path = os.getenv('STATE_PATH', 'state/delivery.json')
    state = load_state(state_path)
    now = datetime.now(timezone.utc)
    if args.demo:
        articles = [Article('示例：LeRobot 机器人学习工具更新', 'https://example.com/robotics',
                            '这是一条离线演示数据，不是真实新闻。实际运行会使用 RSS 原文摘录。', now, '离线样例', 4),
                    Article('示例：AI 智能体研究进展', 'https://example.com/agent',
                            '这是一条离线中文演示数据，未调用大模型 API。', now, '离线样例', 3)]
        status = [{'source': 'offline-demo', 'ok': True, 'count': len(articles)}]
    else:
        articles, status = collect(config, now)
    ranked = rank(articles, config, now)
    title = 'AI 每日新闻 ' + now.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    if args.demo:
        title += '（离线演示）'
    report_dir = Path(os.getenv('REPORT_DIR', 'reports'))
    report_dir.mkdir(parents=True, exist_ok=True)
    page, plain = render(ranked[:config['max_items']], title, config['summary_chars'])
    (report_dir / 'latest.html').write_text(page, encoding='utf-8')
    (report_dir / 'latest.txt').write_text(plain, encoding='utf-8')
    report = {'generated_at': now.isoformat(), 'sources': status, 'candidates': len(ranked),
              'dry_run': dry, 'deliveries': {}}
    failed = False
    if not dry:
        for channel in channels:
            items = pending(ranked, state, channel, config['max_items'])
            if not items:
                report['deliveries'][channel] = {'status': 'no_new_items'}
                continue
            delivery_page, delivery_text = render(items, title, config['summary_chars'])
            # Keep a single free-channel notification comfortably small.
            if channel == 'pushplus':
                while len(delivery_text.encode('utf-8')) > 16000 and items:
                    items = items[:-1]
                    delivery_page, delivery_text = render(items, title, config['summary_chars'])
                if not items:
                    failed = True
                    report['deliveries'][channel] = {'status': 'failed', 'error': 'PayloadTooLarge'}
                    continue
            try:
                receipt = send(channel, title, delivery_page, delivery_text)
            except Exception as exc:
                failed = True
                report['deliveries'][channel] = {'status': 'failed', 'error': type(exc).__name__}
                print(f'ERROR delivery {channel}: {type(exc).__name__} (details hidden to protect secrets)')
                continue
            mark(state, channel, items, now)
            save_state(state_path, state)
            report['deliveries'][channel] = {'status': 'accepted', 'count': len(items), 'receipt': receipt}
            print(f'ACCEPTED {channel}: {len(items)} items')
    (report_dir / 'status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report: {report_dir / "latest.html"}; ranked candidates: {len(ranked)}; dry-run: {dry}')
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # No traceback/response body: those can include SMTP credentials or tokens.
        print(f'FATAL {type(exc).__name__}; check configuration, state and source availability.', file=sys.stderr)
        sys.exit(1)

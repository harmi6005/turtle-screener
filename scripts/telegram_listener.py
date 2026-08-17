# -*- coding: utf-8 -*-
"""텔레그램 폴링 리스너 (GitHub Actions에서 5분마다 자동 실행)

웹훅(webhook_handler.py)이 정상 작동 중이면 이 스크립트는 사실상 할 일이 없어요
(텔레그램은 webhook이 설정되면 getUpdates가 항상 빈 값을 반환합니다).
웹훅이 안 켜져 있거나 실패했을 때를 대비한 백업 경로로 그대로 둡니다.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
from common import notify_telegram, send_long_message
from bot_commands import load_holdings, save_holdings, load_watchlist, save_watchlist, dispatch

OFFSET_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'telegram_offset.txt')


def get_updates(token, offset=None):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {'timeout': 5}
    if offset is not None:
        params['offset'] = offset
    res = requests.get(url, params=params, timeout=15).json()
    return res.get('result', [])


def load_offset():
    if os.path.exists(OFFSET_PATH):
        try:
            return int(open(OFFSET_PATH).read().strip())
        except Exception:
            return None
    return None


def save_offset(offset):
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    with open(OFFSET_PATH, 'w') as f:
        f.write(str(offset))


if __name__ == "__main__":
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("텔레그램 설정이 없어서 명령어 처리를 건너뜁니다.")
        sys.exit(0)

    offset = load_offset()
    updates = get_updates(token, offset)
    if not updates:
        print("새 명령어 없음 (웹훅이 켜져 있으면 항상 이렇게 나오는 게 정상이에요)")
        sys.exit(0)

    df = load_holdings()
    wdf = load_watchlist()
    last_id = offset
    holdings_changed = False
    watchlist_changed = False

    for upd in updates:
        last_id = upd['update_id'] + 1
        msg = upd.get('message', {})
        text = msg.get('text', '').strip()
        if not text:
            continue

        df, wdf, reply, is_long, h_changed, w_changed = dispatch(text, df, wdf)
        holdings_changed = holdings_changed or h_changed
        watchlist_changed = watchlist_changed or w_changed

        if reply:
            if is_long:
                send_long_message(reply)
            else:
                notify_telegram(reply)

    if holdings_changed:
        save_holdings(df)
    if watchlist_changed:
        save_watchlist(wdf)
    save_offset(last_id)
    print("명령어 처리 완료")

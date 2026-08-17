# -*- coding: utf-8 -*-
"""웹훅으로 전달받은 텔레그램 메시지 1개를 즉시 처리 (repository_dispatch로 트리거됨)

Cloudflare Worker가 텔레그램 메시지를 받으면 GitHub의 repository_dispatch API를
호출하고, 그 안의 client_payload.text 값을 이 스크립트가 환경변수(MSG_TEXT)로
받아서 즉시 처리합니다. 5분 폴링을 기다릴 필요 없이 몇 초 안에 처리돼요.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common import notify_telegram, send_long_message
from bot_commands import load_holdings, save_holdings, load_watchlist, save_watchlist, dispatch

if __name__ == "__main__":
    text = os.environ.get('MSG_TEXT', '').strip()
    if not text:
        print("MSG_TEXT가 비어있어서 처리할 게 없습니다.")
        sys.exit(0)

    print(f"수신 메시지: {text}")

    df = load_holdings()
    wdf = load_watchlist()

    df, wdf, reply, is_long, holdings_changed, watchlist_changed = dispatch(text, df, wdf)

    if reply:
        if is_long:
            send_long_message(reply)
        else:
            notify_telegram(reply)
        print(f"답장 전송: {reply[:80]}...")
    else:
        print("인식된 명령어가 아니라서 응답하지 않았습니다.")

    if holdings_changed:
        save_holdings(df)
        print("holdings.csv 업데이트")
    if watchlist_changed:
        save_watchlist(wdf)
        print("watchlist.csv 업데이트")

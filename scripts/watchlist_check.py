# -*- coding: utf-8 -*-
"""감시목록(watchlist) 전용 터틀 신호 체크 (GitHub Actions에서 5분마다 자동 실행)

telegram_listener.py 의 watch/unwatch 명령으로 등록한 종목들을 가격범위 필터 등
상관없이 계속 감시하다가, 관심/진입/청산 신호가 바뀔 때마다 알림을 보냅니다.
System1(단기)/System2(중장기) 둘 다 독립적으로 체크합니다.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from common import SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'watchlist.csv')
WATCHLIST_COLUMNS = ['code', 'market', 'sys1_status', 'sys2_status']


def is_korea_market_open():
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def is_us_market_open():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def get_bithumb_daily_ohlc(coin, days=300):
    url = f"https://api.bithumb.com/public/candlestick/{coin}_KRW/24h"
    res = requests.get(url, timeout=10).json()
    if res.get('status') != '0000':
        return None
    raw = res['data']
    df = pd.DataFrame(raw, columns=['Time', 'Open', 'Close', 'High', 'Low', 'Volume'])
    df['Time'] = pd.to_datetime(df['Time'], unit='ms')
    df = df.set_index('Time')
    for col in ['Open', 'Close', 'High', 'Low', 'Volume']:
        df[col] = df[col].astype(float)
    return df.tail(days)


def get_history(market, code, days=300):
    try:
        if market == 'KR':
            end = datetime.today()
            start = end - timedelta(days=days)
            df = fdr.DataReader(str(code).zfill(6), start, end)
            return df if not df.empty else None
        elif market == 'US':
            df = yf.download(code, period=f'{days}d', auto_adjust=True, progress=False)
            return df if not df.empty else None
        elif market == 'COIN':
            return get_bithumb_daily_ohlc(code, days)
    except Exception:
        return None
    return None


def classify(res):
    if res['entry_signal']:
        return '진입'
    elif res['exit_signal']:
        return '청산'
    elif res['watch_signal']:
        return '관심'
    return ''


if __name__ == "__main__":
    if not os.path.exists(WATCHLIST_PATH):
        print("감시목록 파일이 없어요. 텔레그램에서 watch 명령으로 먼저 등록해주세요.")
        sys.exit(0)

    wdf = pd.read_csv(WATCHLIST_PATH, dtype={'code': str})
    for col in WATCHLIST_COLUMNS:
        if col not in wdf.columns:
            wdf[col] = ''
    wdf = wdf[WATCHLIST_COLUMNS]

    if wdf.empty:
        print("감시목록이 비어있습니다.")
        sys.exit(0)

    kr_open = is_korea_market_open()
    us_open = is_us_market_open()

    changed = False
    for idx, row in wdf.iterrows():
        market, code = row['market'], row['code']
        if market == 'KR' and not kr_open:
            continue
        if market == 'US' and not us_open:
            continue

        df = get_history(market, code)
        if df is None:
            print(f"{code}: 데이터 조회 실패")
            continue

        for sys_key, sys_name in [('sys1_status', 'System1(단기)'), ('sys2_status', 'System2(중장기)')]:
            sysconf = SYSTEMS[sys_name]
            res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
            if not res:
                continue
            new_status = classify(res)
            old_status = str(row[sys_key]) if pd.notna(row[sys_key]) else ''

            if new_status != old_status and new_status != '':
                notify_telegram(
                    f"[감시목록] {code} [{market}] {sys_name} -> {new_status}\n"
                    f"현재가 {res['close']} / N일고가 {res['n_high']} / N일저가 {res['n_low']}"
                )
                print(f"{code} {sys_name}: {old_status or '(없음)'} -> {new_status}")

            wdf.at[idx, sys_key] = new_status
            changed = True

    if changed:
        wdf.to_csv(WATCHLIST_PATH, index=False)
        print("watchlist.csv 업데이트 완료")
    else:
        print("변경 사항 없음")

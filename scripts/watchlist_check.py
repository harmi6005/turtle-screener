# -*- coding: utf-8 -*-
"""감시목록(watchlist / 집중추적종목) 전용 터틀 신호 체크 (GitHub Actions에서
5분마다 자동 실행 — holdings_check.yml 안에서 실행되며, cron-job.org 외부
크론으로 강제 트리거되어 GitHub 자체 스케줄러 지연 문제를 우회함)

텔레그램 `코드 추적시작` 명령으로 등록한 종목들을 보유종목(holdings)과는
완전히 별개로, 가격범위 필터 등과 무관하게 계속 감시합니다.
System1(단기)/System2(중장기) 둘 다 독립적으로 체크합니다.

알림 방식 (2단계, 사용자 요청으로 5분마다 무조건 발송 방식 추가):
1. (기존) 진입/관심/청산 상태가 "직전과 다를 때"만 강조 알림 — 놓치면 안 되는
   전환 시점을 바로 알 수 있도록 즉시 알림
2. (신규) 매 실행마다(5분마다) 등록된 전체 종목의 "현재 상태"를 무조건
   요약 문자로 발송 — 국장/미장은 장중에만 자연히 포함되고(장마감중이면 그
   시장 종목은 이번 요약에서 빠짐), 코인은 24시간 항상 포함됨
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
from common import SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram, send_long_message

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'watchlist.csv')
WATCHLIST_COLUMNS = ['code', 'market', 'sys1_status', 'sys2_status']

STATUS_TAG = {
    '진입': '🟢',
    '관심': '🔶',
    '청산': '⚠️',
    '': '⚪',
}


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
        print("감시목록 파일이 없어요. 텔레그램에서 추적시작 명령으로 먼저 등록해주세요.")
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
    summary_lines = []

    for idx, row in wdf.iterrows():
        market, code = row['market'], row['code']
        if market == 'KR' and not kr_open:
            continue
        if market == 'US' and not us_open:
            continue

        df = get_history(market, code)
        if df is None:
            print(f"{code}: 데이터 조회 실패")
            summary_lines.append(f"- {code} [{market}]: 데이터 조회 실패")
            continue

        code_lines = [f"- {code} [{market}]"]
        for sys_key, sys_name in [('sys1_status', 'System1(단기)'), ('sys2_status', 'System2(중장기)')]:
            sysconf = SYSTEMS[sys_name]
            res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
            if not res:
                code_lines.append(f"    {sys_name}: 데이터 부족")
                continue

            new_status = classify(res)
            old_status = str(row[sys_key]) if pd.notna(row[sys_key]) else ''

            # 1) 상태 전환 강조 알림 (기존 기능, 그대로 유지)
            if new_status != old_status and new_status != '':
                notify_telegram(
                    f"[집중추적] {code} [{market}] {sys_name} -> {new_status}\n"
                    f"현재가 {res['close']} / N일고가 {res['n_high']} / N일저가 {res['n_low']}"
                )
                print(f"{code} {sys_name}: {old_status or '(없음)'} -> {new_status}")

            wdf.at[idx, sys_key] = new_status
            changed = True

            # 2) 매 실행마다 무조건 포함되는 현재 상태 요약용 라인
            gap_pct = (res['close'] - res['n_high']) / res['n_high'] * 100
            tag = STATUS_TAG.get(new_status, '⚪')
            display_status = new_status if new_status else '관찰중'
            code_lines.append(
                f"    {sys_name}: {tag} {display_status} | 현재가 {res['close']} / "
                f"N일고가 {res['n_high']} ({gap_pct:+.2f}%) / N일저가 {res['n_low']}"
            )

        summary_lines.extend(code_lines)

    if changed:
        wdf.to_csv(WATCHLIST_PATH, index=False)
        print("watchlist.csv 업데이트 완료")
    else:
        print("변경 사항 없음")

    # 3) 5분마다 무조건 발송되는 현재 상태 요약 (사용자 요청사항)
    if summary_lines:
        header = f"[집중추적종목 현황] {len(summary_lines)}줄 (5분 자동 갱신)"
        send_long_message(header + "\n" + "\n".join(summary_lines))
    else:
        print("이번 실행에서 포함할 종목이 없어 요약을 보내지 않았습니다 (장마감/데이터없음 등).")


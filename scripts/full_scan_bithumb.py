# -*- coding: utf-8 -*-
"""빗썸 KRW 마켓 전체 코인 스캔 (GitHub Actions에서 지정 시간에 자동 실행)
이미 진입가 대비 너무 많이 오른(0.5% 초과) 코인은 '진입'에서 제외합니다."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import time
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     build_watch_summary, send_long_message, pick_top_entry)

MAX_WORKERS = 10
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_bithumb_result.csv')


def get_bithumb_krw_coins():
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    res = requests.get(url, timeout=10).json()
    data = res.get('data', {})
    return [k for k in data.keys() if k != 'date']


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


def fetch_and_check(coin):
    try:
        df = get_bithumb_daily_ohlc(coin)
        if df is None or df.empty or len(df) < 60:
            return []
    except Exception:
        return []

    rows = []
    for sys_name, sysconf in SYSTEMS.items():
        res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
        if not res:
            continue
        if res['fresh_entry_signal']:
            chase_ratio = (res['close'] - res['n_high']) / res['n_high']
            if chase_ratio > MAX_CHASE_RATIO:
                continue
            signal = '진입'
        elif res['exit_signal']:
            signal = '청산'
        elif res['watch_signal']:
            signal = '관심'
        else:
            continue
        rows.append({'code': coin, 'name': coin, 'system': sys_name, 'signal': signal, **res})
    return rows


def screen_bithumb():
    print("[코인] KRW 마켓 코인 목록 불러오는 중...")
    coins = get_bithumb_krw_coins()
    print(f"총 {len(coins)}개 코인 병렬 조회 시작")

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_and_check, c): c for c in coins}
        for future in as_completed(futures):
            done += 1
            rows = future.result()
            if rows:
                results.extend(rows)
            if done % 50 == 0:
                print(f"  ...{done}/{len(coins)} 완료")

    return pd.DataFrame(results)


if __name__ == "__main__":
    df = screen_bithumb()
    print(f"\n[코인] 신호 코인 {len(df)}개 발견")

    if os.path.exists(DATA_PATH):
        prev_df = pd.read_csv(DATA_PATH)
        confirmed_prev = prev_df[prev_df['signal'] == '확정']
        if not confirmed_prev.empty:
            new_keys = set(zip(df['code'], df['system'])) if not df.empty else set()
            keep_rows = confirmed_prev[~confirmed_prev.apply(
                lambda r: (r['code'], r['system']) in new_keys, axis=1)]
            if not keep_rows.empty:
                df = pd.concat([df, keep_rows], ignore_index=True)
                print(f"기존 확정 코인 {len(keep_rows)}개 보존")

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')
    print(f"결과 저장: {DATA_PATH}")

    entry_cnt = len(df[df['signal'] == '진입']) if not df.empty else 0
    watch_cnt = len(df[df['signal'] == '관심']) if not df.empty else 0

    if entry_cnt > 0:
        top = pick_top_entry(df)
        if top is not None:
            notify_telegram(
                f"[코인 전체스캔] 진입 신호 {entry_cnt}개 중 최신 돌파 1개 픽\n"
                f"- {top['name']} [{top['system']}]\n"
                f"  현재가 {top['close']} / 진입가(돌파) {top['n_high']} / 청산가(손절) {top['n_low']}\n"
                f"  초과율 {top['excess_ratio']*100:.3f}% (돌파강도 ATR배수 {top['strength']:.2f})"
            )

    if watch_cnt > 0:
        summary = build_watch_summary(df, "코인")
        if summary:
            send_long_message(summary)

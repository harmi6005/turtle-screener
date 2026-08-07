# -*- coding: utf-8 -*-
"""미국 주식(S&P500) 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from common import SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_us_result.csv')


def get_sp500_tickers():
    url = 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'
    df = pd.read_csv(url)
    return df['Symbol'].str.replace('.', '-', regex=False).tolist()


def screen_us():
    print("[미국] S&P500 종목 리스트 불러오는 중...")
    tickers = get_sp500_tickers()
    print(f"총 {len(tickers)}개 종목 배치 다운로드 중...")

    end = datetime.today()
    start = end - timedelta(days=180)

    results = []
    data = yf.download(tickers, start=start, end=end, group_by='ticker',
                        auto_adjust=True, threads=True, progress=False)

    for t in tickers:
        try:
            df = data[t].dropna()
            if df.empty or len(df) < 60:
                continue
            for sys_name, sysconf in SYSTEMS.items():
                res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
                if not res:
                    continue
                if res['entry_signal']:
                    signal = '진입'
                elif res['exit_signal']:
                    signal = '청산'
                elif res['watch_signal']:
                    signal = '관심'
                else:
                    continue
                results.append({'code': t, 'name': t, 'system': sys_name, 'signal': signal, **res})
        except Exception:
            continue

    return pd.DataFrame(results)


if __name__ == "__main__":
    df = screen_korea()
    print(f"\n[국내] 신호 종목 {len(df)}개 발견")

    # 기존에 재확인이 '확정'으로 추적 중이던 종목은 유지 (전체스캔이 덮어써서
    # 청산 감시가 끊기지 않도록 보존)
    if os.path.exists(DATA_PATH):
        prev_df = pd.read_csv(DATA_PATH)
        confirmed_prev = prev_df[prev_df['signal'] == '확정']
        if not confirmed_prev.empty:
            new_keys = set(zip(df['code'], df['system'])) if not df.empty else set()
            keep_rows = confirmed_prev[~confirmed_prev.apply(
                lambda r: (r['code'], r['system']) in new_keys, axis=1)]
            if not keep_rows.empty:
                df = pd.concat([df, keep_rows], ignore_index=True)
                print(f"기존 확정 종목 {len(keep_rows)}개 보존")

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')
    print(f"결과 저장: {DATA_PATH}")

    entry_cnt = len(df[df['signal'] == '진입']) if not df.empty else 0
    watch_cnt = len(df[df['signal'] == '관심']) if not df.empty else 0
    if entry_cnt > 0 or watch_cnt > 0:
        notify_telegram(f"[국내 전체스캔 완료]\n진입 {entry_cnt}개 / 관심 {watch_cnt}개")
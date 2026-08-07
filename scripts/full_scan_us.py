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
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    res = requests.get(url, headers=headers, timeout=10)
    table = pd.read_html(res.text)[0]
    return table['Symbol'].str.replace('.', '-', regex=False).tolist()


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
    df = screen_us()
    print(f"\n[미국] 신호 종목 {len(df)}개 발견")

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    df.to_csv(DATA_PATH, index=False)
    print(f"결과 저장: {DATA_PATH}")

    entry_cnt = len(df[df['signal'] == '진입']) if not df.empty else 0
    watch_cnt = len(df[df['signal'] == '관심']) if not df.empty else 0
    notify_telegram(f"[미국 전체스캔 완료]\n진입 {entry_cnt}개 / 관심 {watch_cnt}개")

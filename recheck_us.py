# -*- coding: utf-8 -*-
"""미국 관심종목 재확인 (GitHub Actions에서 5분마다 자동 실행)"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from common import SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_us_result.csv')


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("직전 결과 파일이 없어요. full_scan_us.py를 먼저 실행해주세요.")
        sys.exit(0)

    prev_df = pd.read_csv(DATA_PATH)
    watch_rows = prev_df[prev_df['signal'] == '관심'].to_dict('records')
    print(f"관심종목 {len(watch_rows)}개 재확인 중...")

    if not watch_rows:
        print("현재 관심종목이 없습니다.")
        sys.exit(0)

    tickers = list({r['code'] for r in watch_rows})
    end = datetime.today()
    start = end - timedelta(days=180)

    results = []
    data = yf.download(tickers, start=start, end=end, group_by='ticker',
                        auto_adjust=True, threads=True, progress=False)

    for row in watch_rows:
        code, system = row['code'], row['system']
        sysconf = SYSTEMS.get(system)
        if not sysconf:
            continue
        try:
            df = data[code].dropna() if len(tickers) > 1 else data.dropna()
            if df.empty:
                results.append({'code': code, 'name': code, 'system': system, 'status': '데이터없음'})
                continue
            res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
            if not res:
                results.append({'code': code, 'name': code, 'system': system, 'status': '데이터부족'})
                continue
            status = '확정' if res['entry_signal'] else ('유지' if res['watch_signal'] else '탈락')
            results.append({'code': code, 'name': code, 'system': system, 'status': status, **res})
        except Exception:
            results.append({'code': code, 'name': code, 'system': system, 'status': '오류'})

    result_df = pd.DataFrame(results)
    confirm_df = result_df[result_df['status'] == '확정'] if not result_df.empty else result_df
    print(f"확정 {len(confirm_df)}개")

    for _, r in result_df.iterrows():
        mask = (prev_df['code'] == r['code']) & (prev_df['system'] == r['system'])
        if r['status'] == '확정':
            prev_df.loc[mask, 'signal'] = '확정'
        elif r['status'] == '탈락':
            prev_df.loc[mask, 'signal'] = '탈락'

    prev_df.to_csv(DATA_PATH, index=False)

    if not confirm_df.empty:
        lines = [f"- {r['name']} [{r['system']}] 종가 {r['close']}" for _, r in confirm_df.iterrows()]
        notify_telegram("[미국] 확정 전환 종목!\n" + "\n".join(lines))

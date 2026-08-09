# -*- coding: utf-8 -*-
"""빗썸 관심코인 재확인 (GitHub Actions에서 5분마다 자동 실행)"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram

MAX_WORKERS = 10
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_bithumb_result.csv')


def get_bithumb_daily_ohlc(coin, days=180):
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


def recheck_one(row):
    coin, system, orig_signal = row['code'], row['system'], row['signal']
    sysconf = SYSTEMS.get(system)
    if not sysconf:
        return None
    try:
        df = get_bithumb_daily_ohlc(coin)
        if df is None or df.empty:
            return {'code': coin, 'name': coin, 'system': system, 'status': '데이터없음'}
        res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
        if not res:
            return {'code': coin, 'name': coin, 'system': system, 'status': '데이터부족'}
        if orig_signal == '확정':
            status = '확정이탈' if res['exit_signal'] else '확정유지'
        elif res['entry_signal']:
            chase_ratio = (res['close'] - res['n_high']) / res['n_high']
            status = '스킵(추격과다)' if chase_ratio > MAX_CHASE_RATIO else '확정'
        else:
            status = '유지' if res['watch_signal'] else '탈락'
        return {'code': coin, 'name': coin, 'system': system, 'status': status, **res}
    except Exception:
        return {'code': coin, 'name': coin, 'system': system, 'status': '오류'}


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("직전 결과 파일이 없어요. full_scan_bithumb.py를 먼저 실행해주세요.")
        sys.exit(0)

    prev_df = pd.read_csv(DATA_PATH)
    target_rows = prev_df[prev_df['signal'].isin(['관심', '확정'])].to_dict('records')
    print(f"관심/확정 코인 {len(target_rows)}개 재확인 중...")

    if not target_rows:
        print("현재 추적 중인 코인이 없습니다.")
        sys.exit(0)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(recheck_one, row): row for row in target_rows}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    result_df = pd.DataFrame(results)
    confirm_df = result_df[result_df['status'] == '확정']
    exit_df = result_df[result_df['status'] == '확정이탈']
    skip_df = result_df[result_df['status'] == '스킵(추격과다)']
    print(f"확정 {len(confirm_df)}개 / 확정이탈 {len(exit_df)}개 / 스킵(추격과다) {len(skip_df)}개")

    for _, r in result_df.iterrows():
        mask = (prev_df['code'] == r['code']) & (prev_df['system'] == r['system'])
        if r['status'] in ('확정', '확정유지'):
            prev_df.loc[mask, 'signal'] = '확정'
        elif r['status'] in ('탈락', '스킵(추격과다)'):
            prev_df.loc[mask, 'signal'] = '탈락'
        elif r['status'] == '확정이탈':
            prev_df.loc[mask, 'signal'] = '확정이탈'

    prev_df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')

    if not confirm_df.empty:
        lines = [f"- {r['name']} [{r['system']}]\n"
                 f"  현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}"
                 for _, r in confirm_df.iterrows()]
        notify_telegram("[빗썸] 확정 전환 코인! (매수 검토)\n" + "\n".join(lines))

    if not exit_df.empty:
        lines = [f"- {r['name']} [{r['system']}]\n"
                 f"  현재가 {r['close']} / 청산가(손절) {r['n_low']}"
                 for _, r in exit_df.iterrows()]
        notify_telegram("[빗썸] 확정이탈 코인! (매도 검토)\n" + "\n".join(lines))
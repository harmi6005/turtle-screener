# -*- coding: utf-8 -*-
"""빗썸 관심코인 재확인 (GitHub Actions에서 5분마다 자동 실행)
System1(단기)에는 터틀 휩쏘 필터가 적용됩니다."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     load_trade_history, save_trade_history, check_whipsaw, record_trade_result)

MAX_WORKERS = 10
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_bithumb_result.csv')
HIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trade_history_bithumb.csv')


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
            status = '스킵(추격과다)' if chase_ratio > MAX_CHASE_RATIO else '확정_candidate'
        else:
            status = '유지' if res['watch_signal'] else '탈락'

        return {'code': coin, 'name': coin, 'system': system, 'status': status,
                'entry_price': row.get('entry_price', ''), **res}
    except Exception:
        return {'code': coin, 'name': coin, 'system': system, 'status': '오류'}


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("직전 결과 파일이 없어요. full_scan_bithumb.py를 먼저 실행해주세요.")
        sys.exit(0)

    prev_df = pd.read_csv(DATA_PATH)
    if 'entry_price' not in prev_df = pd.read_csv(DATA_PATH)
    if 'entry_price' not in prev_df.columns:
        prev_df['entry_price'] = pd.Series([None] * len(prev_df), dtype=object)
    else:
        prev_df['entry_price'] = prev_df['entry_price'].astype(object)

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

    hist_df = load_trade_history(HIST_PATH)
    hist_changed = False
    confirm_rows = []
    exit_rows = []
    whipsaw_skip_count = 0

    for r in results:
        if r['status'] == '확정_candidate':
            allowed, hist_df = check_whipsaw(hist_df, r['code'], r['system'], 'long',
                                              r['n_high'], r['close'], r['atr'])
            hist_changed = True
            if allowed:
                r['status'] = '확정'
                confirm_rows.append(r)
            else:
                r['status'] = '관심'
                whipsaw_skip_count += 1
        elif r['status'] == '확정이탈':
            exit_rows.append(r)

    result_df = pd.DataFrame(results)
    confirm_df = pd.DataFrame(confirm_rows)
    exit_df = pd.DataFrame(exit_rows)
    skip_df = result_df[result_df['status'] == '스킵(추격과다)']
    print(f"확정 {len(confirm_df)}개 / 확정이탈 {len(exit_df)}개 / "
          f"휩쏘스킵 {whipsaw_skip_count}개 / 스킵(추격과다) {len(skip_df)}개")

    for r in results:
        code, system, status = r['code'], r['system'], r['status']
        mask = (prev_df['code'] == code) & (prev_df['system'] == system)
        if status == '확정':
            prev_df.loc[mask, 'signal'] = '확정'
            prev_df.loc[mask, 'entry_price'] = r['close']
        elif status in ('탈락', '스킵(추격과다)'):
            prev_df.loc[mask, 'signal'] = '탈락'
        elif status == '확정이탈':
            prev_df.loc[mask, 'signal'] = '확정이탈'
            entry_price = r.get('entry_price', '')
            try:
                entry_price = float(entry_price)
                hist_df = record_trade_result(hist_df, code, system, 'long', entry_price, r['close'])
                hist_changed = True
            except (ValueError, TypeError):
                pass

    prev_df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')
    if hist_changed:
        save_trade_history(hist_df, HIST_PATH)

    if not confirm_df.empty:
        lines = [f"- {r['name']} [{r['system']}]\n"
                 f"  현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
                 f"  괴리율 {(r['close']-r['n_high'])/r['n_high']*100:.2f}%"
                 for _, r in confirm_df.iterrows()]
        notify_telegram("[코인] 확정 전환 코인! (매수 검토)\n" + "\n".join(lines))

    if not exit_df.empty:
        lines = [f"- {r['name']} [{r['system']}]\n"
                 f"  현재가 {r['close']} / 청산가(손절) {r['n_low']}"
                 for _, r in exit_df.iterrows()]
        notify_telegram("[코인] 확정이탈 코인! (매도 검토)\n" + "\n".join(lines))
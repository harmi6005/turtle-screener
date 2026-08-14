# -*- coding: utf-8 -*-
"""미국 관심종목 재확인 (GitHub Actions에서 5분마다 자동 실행)
System1(단기)에는 터틀 휩쏘 필터가 적용됩니다."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     load_trade_history, save_trade_history, check_whipsaw, record_trade_result)

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_us_result.csv')
HIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trade_history_us.csv')


def is_us_market_open():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


if __name__ == "__main__":
    if not is_us_market_open():
        print("미국 장 시간이 아니라서 재확인을 건너뜁니다 (평일 09:30~16:00 ET).")
        sys.exit(0)

    if not os.path.exists(DATA_PATH):
        print("직전 결과 파일이 없어요. full_scan_us.py를 먼저 실행해주세요.")
        sys.exit(0)

    prev_df = pd.read_csv(DATA_PATH)
    if 'entry_price' not in prev_df = pd.read_csv(DATA_PATH)
    if 'entry_price' not in prev_df.columns:
        prev_df['entry_price'] = pd.Series([None] * len(prev_df), dtype=object)
    else:
        prev_df['entry_price'] = prev_df['entry_price'].astype(object)

    target_rows = prev_df[prev_df['signal'].isin(['관심', '확정'])].to_dict('records')
    print(f"관심/확정 종목 {len(target_rows)}개 재확인 중...")

    if not target_rows:
        print("현재 추적 중인 종목이 없습니다.")
        sys.exit(0)

    tickers = list({r['code'] for r in target_rows})
    end = datetime.today()
    start = end - timedelta(days=180)

    data = yf.download(tickers, start=start, end=end, group_by='ticker',
                        auto_adjust=True, threads=True, progress=False)

    results = []
    for row in target_rows:
        code, system, orig_signal = row['code'], row['system'], row['signal']
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

            if orig_signal == '확정':
                status = '확정이탈' if res['exit_signal'] else '확정유지'
            elif res['entry_signal']:
                chase_ratio = (res['close'] - res['n_high']) / res['n_high']
                status = '스킵(추격과다)' if chase_ratio > MAX_CHASE_RATIO else '확정_candidate'
            else:
                status = '유지' if res['watch_signal'] else '탈락'

            results.append({'code': code, 'name': code, 'system': system, 'status': status,
                             'entry_price': row.get('entry_price', ''), **res})
        except Exception:
            results.append({'code': code, 'name': code, 'system': system, 'status': '오류'})

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
    skip_df = result_df[result_df['status'] == '스킵(추격과다)'] if not result_df.empty else result_df
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

    prev_df.to_csv(DATA_PATH, index=False)
    if hist_changed:
        save_trade_history(hist_df, HIST_PATH)

    if not confirm_df.empty:
        lines = [f"- {r['name']} [{r['system']}]\n"
                 f"  현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
                 f"  괴리율 {(r['close']-r['n_high'])/r['n_high']*100:.2f}%"
                 for _, r in confirm_df.iterrows()]
        notify_telegram("[미장] 확정 전환 종목! (매수 검토)\n" + "\n".join(lines))

    if not exit_df.empty:
        lines = [f"- {r['name']} [{r['system']}]\n"
                 f"  현재가 {r['close']} / 청산가(손절) {r['n_low']}"
                 for _, r in exit_df.iterrows()]
        notify_telegram("[미장] 확정이탈 종목! (매도 검토)\n" + "\n".join(lines))
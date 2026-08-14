# -*- coding: utf-8 -*-
"""국내 주식 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)
코스피(KOSPI)만 대상으로 하고, 종가 기준 2만원~6만원 사이 종목만 스캔합니다."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram, build_watch_summary, send_long_message, pick_top_entry

MAX_WORKERS = 20
MARKET = 'KOSPI'
PRICE_MIN = 20000
PRICE_MAX = 60000
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')


def fetch_and_check(code_name, start, end):
    code, name = code_name
    try:
        df = fdr.DataReader(code, start, end)
        if df.empty or len(df) < 60:
            return []
    except Exception:
        return []

    rows = []
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
        rows.append({'code': code, 'name': name, 'system': sys_name, 'signal': signal, **res})
    return rows


def screen_korea():
    print(f"[국장] {MARKET} 종목 리스트 불러오는 중...")
    listing = fdr.StockListing(MARKET)

    before_cnt = len(listing)
    if 'Close' in listing.columns:
        listing = listing[(listing['Close'] >= PRICE_MIN) & (listing['Close'] <= PRICE_MAX)]
        print(f"가격 필터 적용 ({PRICE_MIN:,}~{PRICE_MAX:,}원): {before_cnt}개 -> {len(listing)}개")
    else:
        print("가격 정보(Close 컬럼)를 찾지 못해서 가격 필터 없이 진행합니다.")

    tickers = listing[['Code', 'Name']].values.tolist()
    print(f"총 {len(tickers)}개 종목 병렬 조회 시작")

    end = datetime.today()
    start = end - timedelta(days=180)

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_and_check, cn, start, end): cn for cn in tickers}
        for future in as_completed(futures):
            done += 1
            rows = future.result()
            if rows:
                results.extend(rows)
            if done % 300 == 0:
                print(f"  ...{done}/{len(tickers)} 완료")

    return pd.DataFrame(results)


if __name__ == "__main__":
    df = screen_korea()
    print(f"\n[국장] 신호 종목 {len(df)}개 발견")

    # 기존에 재확인이 '확정'으로 추적 중이던 종목은 유지 (전체스캔이 덮어써서
    # 청산 감시가 끊기지 않도록 보존)
    if os.path.exists(DATA_PATH):
        prev_df = pd.read_csv(DATA_PATH, dtype={'code': str})
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

    if entry_cnt > 0:
        top = pick_top_entry(df)
        if top is not None:
            notify_telegram(
                f"[국장 전체스캔] 진입 신호 {entry_cnt}개 중 최강 1개 픽\n"
                f"- {top['name']}({top['code']}) [{top['system']}]\n"
                f"  현재가 {top['close']} / 진입가(돌파) {top['n_high']} / 청산가(손절) {top['n_low']}\n"
                f"  돌파강도(ATR배수) {top['strength']:.2f}"
            )

    if watch_cnt > 0:
        summary = build_watch_summary(df, "국장")
        if summary:
            send_long_message(summary)
# -*- coding: utf-8 -*-
"""국내 주식 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)

[2026-09-02 변경사항]
- 기존: 스캔 대상 자체를 종가 20,000~60,000원 종목으로 좁혀서 진행
- 변경: 스캔 대상은 코스피(KOSPI) 전체로 확대. 대신 "최종 픽" 단계에서만
  종가 10,000원 이하 조건을 적용해 최대 10개(돌파강도 큰 순)를 알림.
  (기존 20,000원 이상 필터와 신규 10,000원 이하 조건이 정면 충돌하기 때문에
  스캔 단계 필터는 제거하고 최종 픽 단계로 가격조건을 이동함)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import time
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     build_watch_summary, send_long_message, pick_top_entries,
                     PICK_COUNT, PICK_PRICE_MAX, PICK_PRICE_MIN)

MAX_WORKERS = 20
MARKET = 'KOSPI'
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')

KRX_RETRY_COUNT = 3
KRX_RETRY_WAIT_SEC = 15


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
        if res['fresh_entry_signal']:
            chase_ratio = (res['close'] - res['n_high']) / res['n_high']
            if chase_ratio > MAX_CHASE_RATIO:
                continue  # 이미 너무 많이 오른 상태 -> 진입 후보에서 제외
            signal = '진입'
        elif res['exit_signal']:
            signal = '청산'
        elif res['watch_signal']:
            signal = '관심'
        else:
            continue
        rows.append({'code': code, 'name': name, 'system': sys_name, 'signal': signal, **res})
    return rows


def get_kospi_listing():
    """KRX 서버 일시 오류 대응: 15초 간격으로 최대 3회 재시도. 계속 실패하면 None 반환."""
    for attempt in range(1, KRX_RETRY_COUNT + 1):
        try:
            listing = fdr.StockListing(MARKET)
            if listing is not None and not listing.empty:
                return listing
        except Exception as e:
            print(f"KOSPI 목록 조회 실패 ({attempt}/{KRX_RETRY_COUNT}): {e}")
        if attempt < KRX_RETRY_COUNT:
            time.sleep(KRX_RETRY_WAIT_SEC)
    return None


def screen_korea():
    print(f"[국장] {MARKET} 종목 리스트 불러오는 중...")
    listing = get_kospi_listing()
    if listing is None:
        return None

    tickers = listing[['Code', 'Name']].values.tolist()
    print(f"총 {len(tickers)}개 종목 병렬 조회 시작 (가격필터 없이 코스피 전체 스캔)")

    end = datetime.today()
    start = end - timedelta(days=300)

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


def build_pick_message(entry_cnt, top_df):
    lines = [f"[국장 전체스캔] 진입 신호 {entry_cnt}개 중 {PICK_PRICE_MAX:,}원 이하 "
             f"돌파강도 상위 {len(top_df)}픽"]
    for i, (_, r) in enumerate(top_df.iterrows(), 1):
        lines.append(
            f"{i}. {r['name']}({r['code']}) [{r['system']}]\n"
            f"   현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
            f"   돌파강도(ATR배수) {r['strength']:.2f} / 초과율 {r['excess_ratio']*100:.3f}%"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    df = screen_korea()

    if df is None:
        notify_telegram("[국장 전체스캔] 스캔 실패 - KOSPI 종목 리스트를 불러오지 못했습니다 (KRX 서버 오류로 추정).")
        sys.exit(0)

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
        top_df = pick_top_entries(df, top_n=PICK_COUNT, price_max=PICK_PRICE_MAX, price_min=PICK_PRICE_MIN)
        if not top_df.empty:
            send_long_message(build_pick_message(entry_cnt, top_df))
        else:
            notify_telegram(f"[국장 전체스캔] 진입 신호 {entry_cnt}개가 있지만 "
                             f"{PICK_PRICE_MAX:,}원 이하 조건을 만족하는 종목이 없습니다.")
    else:
        notify_telegram("[국장 전체스캔] 실행 완료 - 부합 종목 없음")

    if watch_cnt > 0:
        summary = build_watch_summary(df, "국장")
        if summary:
            send_long_message(summary)
    else:
        notify_telegram("[국장 전체스캔] 관심종목 없음")

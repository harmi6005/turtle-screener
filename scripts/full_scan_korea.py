# -*- coding: utf-8 -*-
"""국내 주식 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)

코스피(KOSPI) 전체 종목을 대상으로 스캔합니다 (가격 필터 없음 - 기존에는
2만원~6만원으로 유니버스 자체를 좁혔으나, 사용자 요청으로 스캔 범위는 전체로
확대했습니다).

단, "최강 1픽"(진입 신호 중 최종 알림으로 뽑는 1개)은 기존처럼 2만원~6만원
가격대 안에 있는 종목 중에서만 고릅니다 (PICK_PRICE_MIN / PICK_PRICE_MAX).
즉 스캔/관심종목 요약은 전체 유니버스 기준, 최종 1픽 알림만 가격대 제한.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram, build_watch_summary, send_long_message, pick_top_entry

MAX_WORKERS = 20
MARKET = 'KOSPI'

# 최종 1픽에만 적용하는 가격 범위 (스캔 유니버스 자체는 더 이상 이 범위로 제한하지 않음)
PICK_PRICE_MIN = 20000
PICK_PRICE_MAX = 60000

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


def screen_korea():
    print(f"[국장] {MARKET} 종목 리스트 불러오는 중...")
    listing = fdr.StockListing(MARKET)
    print(f"스캔 대상: {MARKET} 전체 {len(listing)}개 (가격 필터 없음)")

    tickers = listing[['Code', 'Name']].values.tolist()
    print(f"총 {len(tickers)}개 종목 병렬 조회 시작")

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

    return pd.DataFrame(results), len(tickers)


if __name__ == "__main__":
    df, universe_cnt = screen_korea()
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
        top = pick_top_entry(df, price_min=PICK_PRICE_MIN, price_max=PICK_PRICE_MAX)
        if top is not None:
            t_name = top["name"]
            t_code = top["code"]
            t_system = top["system"]
            t_close = top["close"]
            t_n_high = top["n_high"]
            t_n_low = top["n_low"]
            t_excess = top["excess_ratio"] * 100
            t_strength = top["strength"]
            msg = ("[국장 전체스캔] 진입 신호 " + str(entry_cnt) + "개 중 최신 돌파 1개 픽 "
                   "(가격대 " + f"{PICK_PRICE_MIN:,}" + "~" + f"{PICK_PRICE_MAX:,}" + "원 한정)\n"
                   "- " + str(t_name) + "(" + str(t_code) + ") [" + str(t_system) + "]\n"
                   "  현재가 " + str(t_close) + " / 진입가(돌파) " + str(t_n_high) +
                   " / 청산가(손절) " + str(t_n_low) + "\n"
                   "  초과율 " + format(t_excess, ".3f") + "% (돌파강도 ATR배수 " +
                   format(t_strength, ".2f") + ")")
            notify_telegram(msg)
        else:
            # 진입 신호는 있지만 전부 1픽 가격범위(2만~6만원) 밖인 경우
            notify_telegram(
                "[국장 전체스캔] 진입 신호 " + str(entry_cnt) + "개 있으나 "
                "1픽 가격범위(" + f"{PICK_PRICE_MIN:,}" + "~" + f"{PICK_PRICE_MAX:,}" +
                "원) 안에는 해당 종목 없음"
            )

    if watch_cnt > 0:
        summary = build_watch_summary(df, "국장")
        if summary:
            send_long_message(summary)

    if entry_cnt == 0 and watch_cnt == 0:
        notify_telegram(
            f"[국장 전체스캔] 실행 완료 - 전체 {universe_cnt}개 종목 스캔, "
            f"진입/관심 기준에 부합하는 종목 없음"
        )


# -*- coding: utf-8 -*-
"""국내 주식 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)

- 스캔 유니버스: 코스피(KOSPI) 전체, 가격 필터 없음
- 최종 픽: 진입 신호 중 초과율이 가장 작은 상위 3개(TOP 3)를, 종가 6만원 이하인
  종목 중에서만 선정 (스캔 자체는 가격 제한 없이 전체를 훑되, 알림으로 뽑히는
  종목만 6만원 이하로 제한)
- KRX 종목 리스트 조회 실패 시 15초 간격 최대 3회 재시도, 그래도 실패하면
  죽지 않고 "스캔 실패" 알림 후 정상 종료
- 진입/관심 신호가 0개여도 "실행 완료 - 부합 종목 없음" 알림 발송
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import time
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram, build_watch_summary, send_long_message, pick_top_entry

MAX_WORKERS = 20
MARKET = 'KOSPI'

# 최종 픽(알림)에만 적용되는 가격 제한. 스캔 유니버스 자체에는 적용하지 않음.
PICK_PRICE_MIN = None    # 하한 없음
PICK_PRICE_MAX = 60000   # 종가 6만원 이하만 최종 픽 후보
PICK_TOP_N = 3           # 최종 픽 개수 (기존 1픽 -> 3픽)

LISTING_RETRY_COUNT = 3
LISTING_RETRY_WAIT_SEC = 15

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')


def get_kospi_listing_with_retry():
    """KRX 서버 일시 오류 대비: 15초 간격 최대 3회 재시도. 계속 실패하면 None 반환."""
    last_err = None
    for attempt in range(1, LISTING_RETRY_COUNT + 1):
        try:
            listing = fdr.StockListing(MARKET)
            if listing is not None and not listing.empty:
                return listing
            last_err = "빈 리스트가 반환됨"
        except Exception as e:
            last_err = str(e)
        print(f"[국장] {MARKET} 종목 리스트 조회 실패 ({attempt}/{LISTING_RETRY_COUNT}): {last_err}")
        if attempt < LISTING_RETRY_COUNT:
            time.sleep(LISTING_RETRY_WAIT_SEC)
    return None


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
    listing = get_kospi_listing_with_retry()
    if listing is None:
        return None  # 스캔 자체 실패 (호출부에서 알림 처리)

    tickers = listing[['Code', 'Name']].values.tolist()
    print(f"총 {len(tickers)}개 종목(가격 필터 없이 전체) 병렬 조회 시작")

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


if __name__ == "__main__":
    df = screen_korea()

    if df is None:
        notify_telegram(
            f"[국장 전체스캔] 스캔 실패 - {MARKET} 종목 리스트를 "
            f"{LISTING_RETRY_COUNT}회 재시도했지만 가져오지 못했어요. "
            f"다음 스캔에서 다시 시도됩니다."
        )
        print("[국장] 종목 리스트 조회 실패로 이번 스캔을 건너뜁니다.")
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
        top_df = pick_top_entry(df, price_min=PICK_PRICE_MIN, price_max=PICK_PRICE_MAX, top_n=PICK_TOP_N)
        if top_df is not None and not top_df.empty:
            lines = [
                f"[국장 전체스캔] 진입 신호 {entry_cnt}개 중 "
                f"6만원 이하 상위 {len(top_df)}개 픽"
            ]
            for rank, (_, t) in enumerate(top_df.iterrows(), start=1):
                lines.append(
                    f"{rank}. {t['name']}({t['code']}) [{t['system']}]\n"
                    f"   현재가 {t['close']} / 진입가(돌파) {t['n_high']} / 청산가(손절) {t['n_low']}\n"
                    f"   초과율 {t['excess_ratio']*100:.3f}% (돌파강도 ATR배수 {t['strength']:.2f})"
                )
            notify_telegram("\n".join(lines))
        else:
            notify_telegram(
                f"[국장 전체스캔] 진입 신호 {entry_cnt}개가 있었지만 "
                f"6만원 이하 조건에 맞는 종목이 없어서 픽 없음"
            )

    if watch_cnt > 0:
        summary = build_watch_summary(df, "국장")
        if summary:
            send_long_message(summary)

    if entry_cnt == 0 and watch_cnt == 0:
        notify_telegram("[국장 전체스캔] 실행 완료 - 부합 종목 없음")

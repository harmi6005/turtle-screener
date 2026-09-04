# -*- coding: utf-8 -*-
"""미국 주식(S&P500) 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)
이미 진입가 대비 너무 많이 오른(0.5% 초과) 종목은 '진입'에서 제외합니다.

[2026-09-02 변경사항] 최종 픽을 1개 -> 최대 10개로 확대, 종가 10,000(원화 환산 기준 아님,
단순 통화단위 숫자) 이하 + 돌파강도(ATR배수) 큰 순으로 선정. common.py의 PICK_PRICE_MAX와
동일 임계값을 그대로 사용합니다(국장/코인과 동일 기준 공유)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     build_watch_summary, send_long_message, pick_top_entries,
                     PICK_COUNT, PICK_PRICE_MAX, PICK_PRICE_MIN)

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_us_result.csv')


def get_sp500_tickers():
    url = 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'
    df = pd.read_csv(url)
    return df['Symbol'].str.replace('.', '-', regex=False).tolist()


def screen_us():
    print("[미장] S&P500 종목 리스트 불러오는 중...")
    tickers = get_sp500_tickers()
    print(f"총 {len(tickers)}개 종목 배치 다운로드 중...")

    end = datetime.today()
    start = end - timedelta(days=300)

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
                if res['fresh_entry_signal']:
                    chase_ratio = (res['close'] - res['n_high']) / res['n_high']
                    if chase_ratio > MAX_CHASE_RATIO:
                        continue
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


def build_pick_message(entry_cnt, top_df):
    lines = [f"[미장 전체스캔] 진입 신호 {entry_cnt}개 중 {PICK_PRICE_MAX:,} 이하 "
             f"돌파강도 상위 {len(top_df)}픽 (최대 {PICK_COUNT}픽 중 {len(top_df)}개)"]
    for i, (_, r) in enumerate(top_df.iterrows(), 1):
        lines.append(
            f"{i}. {r['name']} [{r['system']}]\n"
            f"   현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
            f"   돌파강도(ATR배수) {r['strength']:.2f} / 초과율 {r['excess_ratio']*100:.3f}%"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    df = screen_us()
    print(f"\n[미장] 신호 종목 {len(df)}개 발견")

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

    if entry_cnt > 0:
        entry_only_df = df[df['signal'] == '진입']
        price_ok_cnt = len(entry_only_df[entry_only_df['close'] <= PICK_PRICE_MAX]) if PICK_PRICE_MAX is not None else entry_cnt
        print(f"[미장] 진입신호 {entry_cnt}개 중 {PICK_PRICE_MAX:,} 이하 {price_ok_cnt}개 "
              f"(이 중 최대 {PICK_COUNT}개까지 알림)")

        top_df = pick_top_entries(df, top_n=PICK_COUNT, price_max=PICK_PRICE_MAX, price_min=PICK_PRICE_MIN)
        if not top_df.empty:
            # 10개가 안 되더라도(1~9개) 있는 만큼 그대로 발송함
            send_long_message(build_pick_message(entry_cnt, top_df))
        else:
            notify_telegram(f"[미장 전체스캔] 진입 신호 {entry_cnt}개가 있지만 "
                             f"{PICK_PRICE_MAX:,} 이하 조건을 만족하는 종목이 없습니다.")
    else:
        notify_telegram("[미장 전체스캔] 실행 완료 - 부합 종목 없음")

    if watch_cnt > 0:
        summary = build_watch_summary(df, "미장")
        if summary:
            send_long_message(summary)
    else:
        notify_telegram("[미장 전체스캔] 관심종목 없음")

# -*- coding: utf-8 -*-
"""미국 주식(S&P500) 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)
이미 진입가 대비 너무 많이 오른(0.5% 초과) 종목은 '진입'에서 제외합니다.

[2026-09-10 변경사항] "미장알림중지" 반영 (is_market_alert_enabled).
[2026-09-14 변경사항] 전체스캔 on/off(scan_settings.csv) + 휴장일/장 운영시간 밖 스킵.

[2026-10-09 16차 수정 - 전체 교체]
- ⭐ 사용자 요청: 알림은 "확정 전환"만 받기로 함. 전체스캔이 보내던 "진입 신호 상위 픽",
  "실행 완료 - 부합 종목 없음", "관심종목 요약/없음" 알림을 전부 제거함(콘솔 로그만 남김).
  스캔 결과(data/turtle_us_result.csv)에 '진입'으로 기록된 종목은 recheck_us.py가
  5분마다 재확인(추격/휩쏘 필터)해서 통과하면 "확정 전환" 알림을 1회 보냄.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout,
                     is_market_alert_enabled, is_market_open_scan_window)

MARKET_KEY = 'US'
MARKET_CALENDAR = 'XNYS'  # 뉴욕증권거래소 (나스닥 상장 종목도 휴장일은 동일)
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_us_result.csv')
SCAN_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'scan_settings.csv')


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


if __name__ == "__main__":
    # "미장전체스캔중지"/"미장전체스캔시작" 명령으로 스캔 자체를 켜고 끔 (알림 on/off와 별개).
    scan_enabled = is_market_alert_enabled(MARKET_KEY, SCAN_SETTINGS_PATH)
    if not scan_enabled:
        print("[미장] 전체스캔이 꺼져있는 상태입니다 - 이번 실행은 건너뜁니다.")
        sys.exit(0)

    # 휴장일(주말/공휴일)이거나 "개장 1시간 전 ~ 마감 1시간 후" 구간 밖이면 건너뜀.
    if not is_market_open_scan_window(MARKET_CALENDAR, buffer_before_hours=1, buffer_after_hours=1):
        print("[미장] 휴장일이거나 장 운영시간(개장 1시간 전~마감 1시간 후) 밖이라 이번 실행은 건너뜁니다.")
        sys.exit(0)

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
    print(f"[미장] 진입신호 {entry_cnt}개 / 관심신호 {watch_cnt}개 "
          f"(2026-10-09부터 전체스캔은 알림 없음 - '확정 전환'만 recheck_us.py가 알림)")

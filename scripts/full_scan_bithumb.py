# -*- coding: utf-8 -*-
"""빗썸 KRW 마켓 전체 코인 스캔 (GitHub Actions에서 지정 시간에 자동 실행)
이미 진입가 대비 너무 많이 오른(0.5% 초과) 코인은 '진입'에서 제외합니다.

[2026-09-02 변경사항] 최종 픽을 1개 -> 최대 10개로 확대, 종가(원화) 10,000원 이하 +
돌파강도(ATR배수) 큰 순으로 선정.

[2026-09-10 변경사항 - 전체 교체]
- 🐛 버그 수정: "코인알림중지"를 걸어도 전체스캔 알림(진입픽/관심요약/부합없음 등)이
  그대로 발송되던 문제 수정. recheck_bithumb.py와 동일하게 is_market_alert_enabled()를
  체크해서, 알림이 꺼져 있으면 스캔/저장은 그대로 진행하되 텔레그램 발송만 생략함.

[2026-09-24 변경사항 - 전체 교체]
- ⭐ 사용자 요청: 코인은 앞으로 "픽3 종목"과 "내가 지정한 관심 종목(집중추적,
  watchlist_check.py)" 외의 다른 알림은 받지 않기로 함.
  이번 수정으로 전체스캔에서 보내던 아래 3종류의 알림을 전부 제거함(콘솔 로그는
  그대로 남겨 디버깅은 가능):
    ① "관심종목 없음" / 관심종목 돌파임박 요약(build_watch_summary) — 완전 삭제
    ② "실행 완료 - 부합 종목 없음" (진입 신호 자체가 없을 때) — 완전 삭제
    ③ "진입 신호 있지만 가격조건 만족 종목 없음" — 완전 삭제
  이제 코인 전체스캔에서 텔레그램으로 나가는 메시지는 픽(top_df)이 1개 이상
  있을 때의 "[코인 전체스캔] 진입 신호 …픽" 메시지 하나뿐임. 관심/확정 신호는
  data/turtle_bithumb_result.csv에는 계속 기록되지만(recheck_bithumb.py가 내부
  상태 추적용으로 계속 사용), 그 자체로 알림이 나가지는 않음. 사용자가 특정
  코인을 계속 추적하고 싶으면 텔레그램 `코드 추적시작` 명령으로 watchlist에
  등록하면 됨(watchlist_check.py가 별도로 5분마다 체크/알림).
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     send_long_message, pick_top_entries,
                     PICK_COUNT, PICK_PRICE_MAX, PICK_PRICE_MIN,
                     is_market_alert_enabled)

MAX_WORKERS = 10
MARKET_KEY = 'COIN'
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_bithumb_result.csv')
ALERT_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'alert_settings.csv')
SCAN_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'scan_settings.csv')


def get_bithumb_krw_coins():
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    res = requests.get(url, timeout=10).json()
    data = res.get('data', {})
    return [k for k in data.keys() if k != 'date']


def get_bithumb_daily_ohlc(coin, days=300):
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


def fetch_and_check(coin):
    try:
        df = get_bithumb_daily_ohlc(coin)
        if df is None or df.empty or len(df) < 60:
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
                continue
            signal = '진입'
        elif res['exit_signal']:
            signal = '청산'
        elif res['watch_signal']:
            signal = '관심'
        else:
            continue
        rows.append({'code': coin, 'name': coin, 'system': sys_name, 'signal': signal, **res})
    return rows


def screen_bithumb():
    print("[코인] KRW 마켓 코인 목록 불러오는 중...")
    coins = get_bithumb_krw_coins()
    print(f"총 {len(coins)}개 코인 병렬 조회 시작")

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_and_check, c): c for c in coins}
        for future in as_completed(futures):
            done += 1
            rows = future.result()
            if rows:
                results.extend(rows)
            if done % 50 == 0:
                print(f"  ...{done}/{len(coins)} 완료")

    return pd.DataFrame(results)


def build_pick_message(entry_cnt, top_df):
    lines = [f"[코인 전체스캔] 진입 신호 {entry_cnt}개 중 {PICK_PRICE_MAX:,}원 이하 "
             f"돌파강도 상위 {len(top_df)}픽 (최대 {PICK_COUNT}픽 중 {len(top_df)}개)"]
    for i, (_, r) in enumerate(top_df.iterrows(), 1):
        lines.append(
            f"{i}. {r['name']} [{r['system']}]\n"
            f"   현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
            f"   돌파강도(ATR배수) {r['strength']:.2f} / 초과율 {r['excess_ratio']*100:.3f}%"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # 2026-09-14 추가: 매시간 전체스캔 자체를 텔레그램 명령("코인전체스캔중지"/"코인전체스캔시작")
    # 으로 켜고 끌 수 있음. 알림 on/off와는 별개 설정이며, 꺼져 있으면 스캔 자체를 건너뜀.
    scan_enabled = is_market_alert_enabled(MARKET_KEY, SCAN_SETTINGS_PATH)
    if not scan_enabled:
        print("[코인] 매시간 전체스캔이 꺼져있는 상태입니다 - 이번 실행은 건너뜁니다.")
        sys.exit(0)

    alerts_enabled = is_market_alert_enabled(MARKET_KEY, ALERT_SETTINGS_PATH)
    if not alerts_enabled:
        print("[코인] 알림 중지 상태입니다 - 스캔/저장은 정상 진행하되 텔레그램 발송만 생략합니다.")

    def notify_long(text):
        if alerts_enabled:
            send_long_message(text)

    df = screen_bithumb()
    print(f"\n[코인] 신호 코인 {len(df)}개 발견")

    if os.path.exists(DATA_PATH):
        prev_df = pd.read_csv(DATA_PATH)
        confirmed_prev = prev_df[prev_df['signal'] == '확정']
        if not confirmed_prev.empty:
            new_keys = set(zip(df['code'], df['system'])) if not df.empty else set()
            keep_rows = confirmed_prev[~confirmed_prev.apply(
                lambda r: (r['code'], r['system']) in new_keys, axis=1)]
            if not keep_rows.empty:
                df = pd.concat([df, keep_rows], ignore_index=True)
                print(f"기존 확정 코인 {len(keep_rows)}개 보존")

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')
    print(f"결과 저장: {DATA_PATH}")

    entry_cnt = len(df[df['signal'] == '진입']) if not df.empty else 0
    watch_cnt = len(df[df['signal'] == '관심']) if not df.empty else 0
    print(f"[코인] 진입신호 {entry_cnt}개 / 관심신호 {watch_cnt}개 "
          f"(2026-09-24부터 픽3 외 알림은 전송하지 않음 - 콘솔 로그로만 확인)")

    # 2026-09-24: 픽(top_df)이 있을 때만 딱 1건 알림. 그 외(진입 0개, 가격조건
    # 미달, 관심종목 등) 어떤 경우에도 텔레그램 알림을 보내지 않음.
    if entry_cnt > 0:
        top_df = pick_top_entries(df, top_n=PICK_COUNT, price_max=PICK_PRICE_MAX, price_min=PICK_PRICE_MIN)
        if not top_df.empty:
            notify_long(build_pick_message(entry_cnt, top_df))
        else:
            print(f"[코인] 진입 신호 {entry_cnt}개가 있지만 {PICK_PRICE_MAX:,}원 이하 "
                  f"조건을 만족하는 종목이 없어 알림을 보내지 않습니다.")

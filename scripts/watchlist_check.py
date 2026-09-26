# -*- coding: utf-8 -*-
"""감시목록(watchlist / 집중추적종목) 전용 터틀 신호 체크 (GitHub Actions에서
5분마다 자동 실행 — holdings_check.yml 안에서 실행되며, cron-job.org 외부
크론으로 강제 트리거되어 GitHub 자체 스케줄러 지연 문제를 우회함)

텔레그램 `코드 추적시작` 명령으로 등록한 종목들을 보유종목(holdings)과는
완전히 별개로, 가격범위 필터 등과 무관하게 계속 감시합니다.
System1(단기)/System2(중장기) 둘 다 독립적으로 체크합니다.

알림 방식 (2단계, 사용자 요청으로 5분마다 무조건 발송 방식 추가):
1. (기존) 상태가 "직전과 다를 때"만 강조 알림 — 놓치면 안 되는 전환 시점을
   바로 알 수 있도록 즉시 알림
2. (신규) 매 실행마다(5분마다) 등록된 전체 종목의 "현재 상태"를 무조건
   요약 문자로 발송 — 국장/미장은 장중에만 자연히 포함되고(장마감중이면 그
   시장 종목은 이번 요약에서 빠짐), 코인은 24시간 항상 포함됨

[2026-09-24 변경사항 - 전체 교체]
- 🐛 is_korea_market_open()이 "평일 9:00~15:30"만 봤을 뿐 설/추석 같은 명절이나
  임시공휴일은 몰라서, 평일 공휴일에도 국장 종목이 계속 "장중"으로 포함되어
  5분마다 집중추적 요약 문자가 하루 종일 발송되고 있었음.
- common.py에 새로 추가된 is_korea_trading_day()(exchange_calendars로 KRX
  실제 개장일을 정확히 판정 - 주말+공휴일 모두 포함)를 먼저 확인하도록 수정.
  requirements.txt에 exchange_calendars가 추가되어 있어야 정상 동작하며(누락 시
  주말 여부만으로 안전하게 폴백). 미장(is_us_market_open)은 이번 수정 대상이
  아니며 기존 그대로 유지함.

[2026-09-24 변경사항 - 전체 교체 (이어서)]
- 🐛 GitHub Actions 로그에서 발견된 실행 오류 수정: TypeError: Invalid value ''
  for dtype 'float64'. 원인은 "코드 추적시작"으로 종목을 처음 등록하면
  sys1_status/sys2_status가 빈 문자열('')로 저장되는데, watchlist.csv를 다시
  읽어올 때 그 칸이 비어있으면 pandas가 값이 하나도 없는 컬럼이라고 판단해 해당
  컬럼 dtype을 자동으로 float64(빈칸=NaN)로 추론해버림. 이후
  wdf.at[idx, sys_key] = new_status로 '관심'/'진입' 같은 문자열이나 빈 문자열을
  다시 써넣으려 하면, pandas가 "float64 컬럼에 문자열은 넣을 수 없다"며 곧바로
  예외를 던져 워크플로우 전체가 실패했음(추적 등록 직후 최초 실행에서 특히 잘 남).
  pd.read_csv에 sys1_status/sys2_status도 dtype=str로 명시하고
  keep_default_na=False를 추가해서, 빈 칸을 NaN이 아닌 빈 문자열 그대로(컬럼
  dtype은 항상 문자열로) 읽어오도록 수정. bot_commands.py의 load_watchlist()도
  "코드 추적시작" 직후 같은 파일을 다시 읽는 경로라 동일하게 수정함.

[2026-09-24 변경사항 - 전체 교체 (이어서, 2차)]
- ⭐ 사용자 요청: "지정한 종목 추적은 관찰만 하는 게 아니라 규칙에 따라 진입과
  보류 신호도 줘야 한다." 기존에는 classify()가 추격필터/휩쏘필터 없이
  "종가가 N일 최고가 이상이면 무조건 진입"으로만 판정해서, full_scan/recheck가
  실제로 쓰는 규칙보다 단순했음.
- common.py에 새로 추가된 classify_with_whipsaw()로 교체: full_scan/recheck와
  완전히 동일한 규칙(추격필터+휩쏘필터)으로 '진입확정'(매수 신호) / '보류'
  (휩쏘필터로 이번 1회 보류 중) / '관심'(돌파임박) / '청산'(매도 신호) /
  ''(관찰중) 5단계 상태를 판정함.
- 휩쏘 필터 상태(직전 거래 승패)를 기억하기 위해 data/trade_history_watchlist.csv
  신규 생성(recheck_korea.py/recheck_bithumb.py가 쓰는 이력 파일과는 완전히
  별개 - 전체스캔 결과와 집중추적 결과가 서로 승패 기록을 오염시키지 않도록 분리).
- watchlist.csv에 sys1_entry_price/sys2_entry_price 컬럼 신규 추가: '진입확정'
  시점의 체결가(종가)를 저장해뒀다가, 나중에 '청산'으로 전환될 때 손익(승/패)을
  계산해서 휩쏘 이력에 기록하는 데 사용함.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from common import (SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram,
                     send_long_message, is_korea_trading_day, classify_with_whipsaw,
                     load_trade_history, save_trade_history, record_trade_result)

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'watchlist.csv')
WATCHLIST_COLUMNS = ['code', 'market', 'sys1_status', 'sys2_status',
                      'sys1_entry_price', 'sys2_entry_price']
# 2026-09-24 신규: 전체스캔(recheck_*.py)의 휩쏘 이력과 분리된, 집중추적 전용 이력.
HIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trade_history_watchlist.csv')

STATUS_TAG = {
    '진입확정': '🟢',
    '보류': '🟠',
    '관심': '🔶',
    '청산': '⚠️',
    '': '⚪',
}
STATUS_DESC = {
    '진입확정': '매수 신호 (휩쏘필터 통과)',
    '보류': '돌파는 발생했지만 휩쏘필터로 이번 1회 보류',
    '관심': '돌파임박 (관찰)',
    '청산': '매도 신호',
    '': '관찰중',
}


def is_korea_market_open():
    """2026-09-24 수정: 시간대 체크 전에 먼저 실제 개장일(주말+공휴일)인지부터 확인."""
    if not is_korea_trading_day():
        return False
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def is_us_market_open():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


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


def get_history(market, code, days=300):
    try:
        if market == 'KR':
            end = datetime.today()
            start = end - timedelta(days=days)
            df = fdr.DataReader(str(code).zfill(6), start, end)
            return df if not df.empty else None
        elif market == 'US':
            df = yf.download(code, period=f'{days}d', auto_adjust=True, progress=False)
            return df if not df.empty else None
        elif market == 'COIN':
            return get_bithumb_daily_ohlc(code, days)
    except Exception:
        return None
    return None


if __name__ == "__main__":
    if not os.path.exists(WATCHLIST_PATH):
        print("감시목록 파일이 없어요. 텔레그램에서 추적시작 명령으로 먼저 등록해주세요.")
        sys.exit(0)

    wdf = pd.read_csv(WATCHLIST_PATH, dtype={'code': str, 'market': str,
                                              'sys1_status': str, 'sys2_status': str,
                                              'sys1_entry_price': str, 'sys2_entry_price': str},
                      keep_default_na=False)
    for col in WATCHLIST_COLUMNS:
        if col not in wdf.columns:
            wdf[col] = ''
    wdf = wdf[WATCHLIST_COLUMNS]

    if wdf.empty:
        print("감시목록이 비어있습니다.")
        sys.exit(0)

    hist_df = load_trade_history(HIST_PATH)
    hist_changed = False

    kr_open = is_korea_market_open()
    us_open = is_us_market_open()

    changed = False
    summary_lines = []
    tracked_code_count = 0

    for idx, row in wdf.iterrows():
        market, code = row['market'], row['code']
        if market == 'KR' and not kr_open:
            continue
        if market == 'US' and not us_open:
            continue

        df = get_history(market, code)
        if df is None:
            print(f"{code}: 데이터 조회 실패")
            summary_lines.append(f"- {code} [{market}]: 데이터 조회 실패")
            tracked_code_count += 1
            continue

        tracked_code_count += 1
        code_lines = [f"- {code} [{market}]"]
        for sys_key, price_key, sys_name in [
            ('sys1_status', 'sys1_entry_price', 'System1(단기)'),
            ('sys2_status', 'sys2_entry_price', 'System2(중장기)'),
        ]:
            sysconf = SYSTEMS[sys_name]
            res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
            if not res:
                code_lines.append(f"    {sys_name}: 데이터 부족")
                continue

            old_status = str(row[sys_key]) if pd.notna(row[sys_key]) else ''
            old_entry_price = str(row[price_key]) if pd.notna(row[price_key]) else ''

            new_status, hist_df = classify_with_whipsaw(res, hist_df, code, sys_name)

            # 진입확정 -> 청산으로 전환되는 순간, 저장해둔 체결가로 승/패를
            # 판정해서 휩쏘 이력에 기록한다 (다음 신규 돌파의 보류 여부에 반영됨).
            if old_status == '진입확정' and new_status == '청산':
                try:
                    ep = float(old_entry_price)
                    hist_df = record_trade_result(hist_df, code, sys_name, 'long', ep, res['close'])
                    hist_changed = True
                except (TypeError, ValueError):
                    pass  # 체결가가 없던 옛날 데이터는 승/패 기록 없이 넘어감

            # 1) 상태 전환 강조 알림 - '보류'도 포함해서, 사용자가 "왜 아직 매수
            #    신호가 안 오는지"(휩쏘필터로 보류 중임)를 바로 알 수 있게 함.
            if new_status != old_status and new_status != '':
                notify_telegram(
                    f"[집중추적] {code} [{market}] {sys_name} -> {new_status} "
                    f"({STATUS_DESC.get(new_status, '')})\n"
                    f"현재가 {res['close']} / N일고가 {res['n_high']} / N일저가 {res['n_low']}"
                )
                print(f"{code} {sys_name}: {old_status or '(없음)'} -> {new_status}")

            # 진입확정 시점의 체결가(종가)를 저장해서 나중에 청산 시 손익 판정에 사용
            if new_status == '진입확정':
                new_entry_price = str(res['close'])
            elif new_status in ('청산', ''):
                new_entry_price = ''
            else:
                new_entry_price = old_entry_price

            wdf.at[idx, sys_key] = new_status
            wdf.at[idx, price_key] = new_entry_price
            changed = True

            # 2) 매 실행마다 무조건 포함되는 현재 상태 요약용 라인
            gap_pct = (res['close'] - res['n_high']) / res['n_high'] * 100
            tag = STATUS_TAG.get(new_status, '⚪')
            display_status = STATUS_DESC.get(new_status, new_status or '관찰중')
            code_lines.append(
                f"    {sys_name}: {tag} {new_status or '관찰중'} ({display_status}) | "
                f"현재가 {res['close']} / N일고가 {res['n_high']} ({gap_pct:+.2f}%) / "
                f"N일저가 {res['n_low']}"
            )

        summary_lines.extend(code_lines)

    if changed:
        wdf.to_csv(WATCHLIST_PATH, index=False)
        print("watchlist.csv 업데이트 완료")
    else:
        print("변경 사항 없음")

    if hist_changed:
        save_trade_history(hist_df, HIST_PATH)

    # 3) 5분마다 무조건 발송되는 현재 상태 요약 (사용자 요청사항)
    if summary_lines:
        header = f"🎯 [집중추적종목 현황] {tracked_code_count}종목 (5분 자동 갱신)"
        send_long_message(header + "\n" + "\n".join(summary_lines))
    else:
        print("이번 실행에서 포함할 종목이 없어 요약을 보내지 않았습니다 (장마감/공휴일/데이터없음 등).")

# -*- coding: utf-8 -*-
"""보유종목 트레일링 손절 + ATR 배수 수익 알림 + 5분마다 현황 요약
(GitHub Actions에서 5분마다 자동 실행, cron-job.org 외부 크론으로 강제 트리거됨)

telegram_listener.py / webhook_handler.py 가 등록한 data/holdings.csv 의 거래(trade_id)들을
감시하다가,
- 오늘 최고가가 이전 최고가를 갱신하면 -> 손절선도 "새 최고가 - 2xATR"로 같이 올림 (트레일링)
- 진입가 대비 ATR의 정수배(1배,2배,3배...)만큼 새로 오르면 -> "N배 수익 도달" 알림 (매도신호 아님)
- 오늘 저가가 손절선 밑으로 떨어지면 -> "트레일링 손절 도달" 알림 (매도 검토, 최초 1회만 발송)

⚠️ 손절선 이탈 이후에도 감시를 멈추지 않습니다. 손절 알림은 최초 1회만
보내고(반복 스팸 방지), 이후에는 사용자가 텔레그램으로 `sell 거래번호`를
직접 보내서 수동 청산(status=closed_manual)하기 전까지 계속 5분마다
"[보유종목 현황]" 요약에 "손절 확정 (매도 대기)" 상태로 포함되며 트레일링도
계속 갱신됩니다. (예전에는 손절 이탈 즉시 status=stop_hit이 되면서 감시
대상에서 완전히 빠져버리는 문제가 있었음 — 이번에 수정함)

추가로, 매 실행마다(5분마다) 이변이 없어도 활성 보유종목 전체의 현재 상태를
"[보유종목 현황]" 요약으로 무조건 발송합니다. 국장/미장은 아래 개장시간
버퍼(전후 5분씩)에 걸릴 때만 자연히 포함되고, 코인은 24시간 항상 포함됩니다.

개장시간 버퍼:
- 국장(KR): 08:55~15:35 KST (정규장 09:00~15:30 기준 앞뒤 5분씩)
- 미장(US): 09:25~16:05 ET (정규장 09:30~16:00 기준 앞뒤 5분씩)
- 개장 전 버퍼 시간대에는 시세 API가 아직 당일 데이터를 안 주기 때문에
  전일 종가 기준으로 표시됨 (참고용, 실시간가 아님)
- 마감 후 버퍼 시간대는 당일 최종 종가/고저가가 확정된 뒤의 마지막 확인
  용도로, 장마감 시점에 딱 걸려서 그날의 마지막 현황 문자가 누락되는 것을
  방지하기 위함 (개장 전 버퍼와 대칭으로 추가)

data/holdings.csv 컬럼:
  trade_id,market,code,buy_price,atr_entry,highest_price,stop_price,last_milestone,status
  market 값: KR(국내) / US(미국) / COIN(빗썸)
  status 값: active(감시중) / stop_hit(손절도달, 매도 대기중 - 계속 감시함) / closed_manual(수동청산, 감시종료)
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
from common import notify_telegram, send_long_message, calc_atr

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'holdings.csv')
COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'atr_entry',
           'highest_price', 'stop_price', 'last_milestone', 'status']
NUMERIC_COLUMNS = ['buy_price', 'atr_entry', 'highest_price', 'stop_price', 'last_milestone']
ATR_MULTIPLIER = 2

# 개장 전/마감 후 버퍼 (분 단위, 대칭)
PRE_MARKET_BUFFER_MIN = 5
POST_MARKET_BUFFER_MIN = 5


def fmt_num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    if pd.isna(v):
        return "N/A"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip('0').rstrip('.')


def _in_buffered_window(now, open_t, close_t, pre_buffer_min, post_buffer_min):
    """오늘 날짜 기준으로 open_t(개장)~close_t(마감) 앞뒤로 버퍼(분)를 준 시간대
    안에 now가 포함되는지 확인한다."""
    base_date = now.date()
    window_start = datetime.combine(base_date, open_t) - timedelta(minutes=pre_buffer_min)
    window_end = datetime.combine(base_date, close_t) + timedelta(minutes=post_buffer_min)
    now_naive = datetime.combine(base_date, now.time())
    return window_start <= now_naive <= window_end


def is_korea_market_open():
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    if now.weekday() >= 5:
        return False
    return _in_buffered_window(now, dtime(9, 0), dtime(15, 30),
                                PRE_MARKET_BUFFER_MIN, POST_MARKET_BUFFER_MIN)


def is_us_market_open():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    return _in_buffered_window(now, dtime(9, 30), dtime(16, 0),
                                PRE_MARKET_BUFFER_MIN, POST_MARKET_BUFFER_MIN)


def get_bithumb_daily_ohlc(coin, days=5):
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


def get_latest_ohlc(market, code):
    """시장별로 최근 캔들(오늘 포함)의 고가/저가/종가를 가져온다."""
    try:
        if market == 'KR':
            end = datetime.today()
            start = end - timedelta(days=10)
            df = fdr.DataReader(str(code).zfill(6), start, end)
            if df.empty:
                return None
            last = df.iloc[-1]
            return {'high': last['High'], 'low': last['Low'], 'close': last['Close']}

        elif market == 'US':
            data = yf.download(code, period='5d', auto_adjust=True, progress=False)
            if data.empty:
                return None
            last = data.iloc[-1]
            return {'high': float(last['High']), 'low': float(last['Low']), 'close': float(last['Close'])}

        elif market == 'COIN':
            df = get_bithumb_daily_ohlc(code, days=5)
            if df is None or df.empty:
                return None
            last = df.iloc[-1]
            return {'high': last['High'], 'low': last['Low'], 'close': last['Close']}
    except Exception as e:
        print(f"  {code} 조회 실패: {e}")
        return None
    return None


def build_status_tag(current_price, stop_price):
    """손절선까지 괴리율 기준으로 상태 태그를 부여한다."""
    if stop_price is None or pd.isna(stop_price) or stop_price == 0:
        return "🟡 손절가 미설정"
    if current_price <= stop_price:
        return "⚠️ 손절선 이탈"
    gap_pct = (current_price - stop_price) / stop_price * 100
    if gap_pct <= 2.0:
        return "🔶 손절선 임박"
    return "🟢 정상"


def estimate_atr(market, code, period=20):
    """holdings.csv에 atr_entry가 비어있는(NaN) 종목을 위한 ATR 재계산.
    get_latest_ohlc는 최근 5~10일치만 가져와서 ATR(20일) 계산엔 부족하므로
    별도로 더 긴 기간의 데이터를 조회한다."""
    try:
        if market == 'KR':
            end = datetime.today()
            start = end - timedelta(days=period + 30)
            df = fdr.DataReader(str(code).zfill(6), start, end)
        elif market == 'US':
            df = yf.download(code, period=f'{period + 30}d', auto_adjust=True, progress=False)
        elif market == 'COIN':
            df = get_bithumb_daily_ohlc(code, days=period + 30)
        else:
            return None
        if df is None or df.empty or len(df) < period + 1:
            return None
        atr_series = calc_atr(df, period)
        val = atr_series.iloc[-1]
        return None if pd.isna(val) else float(val)
    except Exception as e:
        print(f"  {code} ATR 재계산 실패: {e}")
        return None


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("data/holdings.csv 파일이 없어요. 텔레그램에서 buy 명령으로 먼저 등록해주세요.")
        sys.exit(0)

    df = pd.read_csv(DATA_PATH, dtype={'code': str, 'trade_id': str})
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ''
    df = df[COLUMNS].copy()

    # ⚠️ 핵심 수정 부분: CSV를 읽으면 숫자 컬럼도 문자열(object/string) dtype으로
    # 인식되는 경우가 있어서, 이후 df.at[idx, col] = float값 대입 시
    # "Invalid value ... for dtype 'str'" 오류가 발생했음.
    # 여기서 미리 숫자형으로 강제 변환해서 원천 차단.
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['last_milestone'] = df['last_milestone'].fillna(0)

    if df.empty:
        print("등록된 거래가 없습니다.")
        sys.exit(0)

    # ⚠️ 데이터 보정: atr_entry / stop_price가 비어있는(NaN) 종목이 있으면
    # (등록 당시 ATR 조회 실패, 수동 편집 등으로 값이 빠진 경우) 재계산해서
    # CSV 자체를 복구한다. 이걸 안 하면 이후 fmt_num/트레일링 계산에서
    # NaN 때문에 스크립트가 죽을 수 있음.
    repaired = False
    for idx, row in df.iterrows():
        if row.get('status') == 'closed_manual':
            continue

        atr_entry = row['atr_entry']
        stop_price = row['stop_price']
        buy_price = row['buy_price']
        highest_price = row['highest_price'] if not pd.isna(row['highest_price']) else buy_price

        if pd.isna(atr_entry) or atr_entry <= 0:
            new_atr = estimate_atr(row['market'], row['code'])
            if new_atr is not None:
                atr_entry = new_atr
                df.at[idx, 'atr_entry'] = round(atr_entry, 6)
                repaired = True
                print(f"거래 {row['trade_id']}({row['code']}) ATR 값 재계산: {atr_entry}")

        if pd.isna(stop_price) and not pd.isna(atr_entry) and atr_entry > 0:
            new_stop = highest_price - ATR_MULTIPLIER * atr_entry
            df.at[idx, 'stop_price'] = round(float(new_stop), 4)
            repaired = True
            print(f"거래 {row['trade_id']}({row['code']}) 손절가 재계산: {new_stop}")

    if repaired:
        df.to_csv(DATA_PATH, index=False)
        print("holdings.csv 데이터 보정 완료 (누락된 ATR/손절가 재계산)")

    kr_open = is_korea_market_open()
    us_open = is_us_market_open()

    changed = False
    summary_lines = []

    for idx, row in df.iterrows():
        current_status = row.get('status', 'active')
        if current_status == 'closed_manual':
            continue  # 사용자가 sell로 직접 청산한 거래만 감시 대상에서 제외

        market = row['market']
        if market == 'KR' and not kr_open:
            continue
        if market == 'US' and not us_open:
            continue
        # COIN은 24시간이라 항상 체크

        ohlc = get_latest_ohlc(market, row['code'])
        if ohlc is None:
            continue

        trade_id = row['trade_id']
        code = row['code']
        buy_price = float(row['buy_price'])
        atr_entry = row['atr_entry']
        atr_entry = float(atr_entry) if not pd.isna(atr_entry) else float('nan')
        highest_price = float(row['highest_price'])
        stop_price = row['stop_price']
        stop_price = float(stop_price) if not pd.isna(stop_price) else float('nan')
        last_milestone = int(row['last_milestone']) if pd.notna(row['last_milestone']) else 0
        already_stopped = (current_status == 'stop_hit')

        # 1) 최고가 갱신 -> 트레일링 손절선 갱신 (내려가지는 않음)
        # 손절 이탈 이후(매도 대기중)에도 계속 갱신함 - sell 하기 전까지 감시를 멈추지 않음
        # ATR/손절가를 위 보정 단계에서도 못 구한 경우(데이터 조회 실패 등)는
        # 트레일링 계산을 건너뛰고 감시만 계속한다 (죽지 않도록 방어).
        if ohlc['high'] > highest_price:
            highest_price = ohlc['high']
            if not pd.isna(atr_entry):
                new_stop = highest_price - ATR_MULTIPLIER * atr_entry
                if pd.isna(stop_price) or new_stop > stop_price:
                    stop_price = new_stop
                    df.at[idx, 'stop_price'] = round(float(stop_price), 4)
            df.at[idx, 'highest_price'] = float(highest_price)
            changed = True

        # 2) ATR 배수 마일스톤 체크 (매도 신호 아님, 진행 알림)
        if not pd.isna(atr_entry) and atr_entry > 0:
            current_multiple = int((highest_price - buy_price) // atr_entry)
            if current_multiple > last_milestone:
                profit_pct = (highest_price - buy_price) / buy_price * 100
                notify_telegram(
                    f"[{market}] {current_multiple}배 수익 도달 (진행상황)\n"
                    f"거래번호 {trade_id} - {code}\n"
                    f"매수가 {fmt_num(buy_price)} / 현재 최고가 {fmt_num(highest_price)}\n"
                    f"수익률 {profit_pct:+.2f}% / 현재 손절선 {fmt_num(stop_price)}"
                )
                df.at[idx, 'last_milestone'] = int(current_multiple)
                last_milestone = current_multiple
                changed = True
                print(f"거래 {trade_id}({code}) {current_multiple}배 마일스톤 알림 전송")

        # 3) 트레일링 손절선 이탈 체크 (진짜 매도 신호)
        # 이미 손절 상태(stop_hit)였다면 알림은 최초 1회만 보내고, 이후엔
        # 반복 알림 없이 계속 감시만 함 (sell 명령 전까지 매도 대기 상태 유지)
        # 손절가를 아직 못 구한 경우(NaN)는 손절 판정 자체를 건너뛴다.
        stop_condition = (not pd.isna(stop_price)) and (ohlc['low'] <= stop_price)
        newly_stopped = stop_condition and not already_stopped

        if newly_stopped:
            pnl_pct = (ohlc['close'] - buy_price) / buy_price * 100
            notify_telegram(
                f"[{market}] 트레일링 손절 도달! (매도 검토)\n"
                f"거래번호 {trade_id} - {code}\n"
                f"매수가 {fmt_num(buy_price)} / 최고가 {fmt_num(highest_price)} / "
                f"손절선 {fmt_num(stop_price)} / 현재가 {fmt_num(ohlc['close'])}\n"
                f"손익률 {pnl_pct:+.2f}%\n"
                f"※ sell {trade_id} 명령으로 청산 전까지 계속 추적합니다."
            )
            df.at[idx, 'status'] = 'stop_hit'
            changed = True
            print(f"거래 {trade_id}({code}) 트레일링 손절 최초 도달 알림 전송")
        elif stop_condition and already_stopped:
            print(f"거래 {trade_id}({code}): 손절 확정 상태 유지 중 (매도 대기, 반복 알림 생략)")
        else:
            print(f"거래 {trade_id}({code}): 현재가 {ohlc['close']} "
                  f"(최고가 {highest_price} / 손절선 {fmt_num(stop_price)}) - 감시 유지")

        # 4) 5분마다 무조건 포함되는 현재 상태 요약용 라인
        # 손절 확정 상태(stop_hit)도 sell 하기 전까지는 계속 요약에 포함시킴
        pnl_pct = (ohlc['close'] - buy_price) / buy_price * 100
        if stop_price and not pd.isna(stop_price):
            gap_to_stop_pct = (ohlc['close'] - stop_price) / stop_price * 100
        else:
            gap_to_stop_pct = 0.0
        if atr_entry and not pd.isna(atr_entry) and atr_entry > 0:
            atr_multiple = (highest_price - buy_price) / atr_entry
        else:
            atr_multiple = 0.0
        if newly_stopped or already_stopped:
            status_tag = "🔴 손절 확정 (sell 명령 대기)"
        else:
            status_tag = build_status_tag(ohlc['close'], stop_price)
        summary_lines.append(
            f"- [{trade_id}] {code} [{market}] {status_tag}\n"
            f"  현재가 {fmt_num(ohlc['close'])} / 매수가 {fmt_num(buy_price)} (손익 {pnl_pct:+.2f}%)\n"
            f"  최고가 {fmt_num(highest_price)} / 손절선 {fmt_num(stop_price)} (괴리율 {gap_to_stop_pct:+.2f}%)\n"
            f"  ATR배수 {atr_multiple:+.2f}배 (직전 마일스톤 {last_milestone}배)"
        )

    if changed:
        df.to_csv(DATA_PATH, index=False)
        print("holdings.csv 상태 업데이트 완료")
    else:
        print("변경 사항 없음")

    if summary_lines:
        header = f"[보유종목 현황] {len(summary_lines)}건"
        send_long_message(header + "\n" + "\n".join(summary_lines))
    else:
        print("이번 실행에서 포함할 활성 보유종목이 없어 현황 요약을 보내지 않았습니다.")


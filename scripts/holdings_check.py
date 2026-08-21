# -*- coding: utf-8 -*-
"""보유종목 트레일링 손절 + ATR 배수 수익 알림 + 5분마다 현황 요약
(GitHub Actions에서 5분마다 자동 실행, cron-job.org 외부 크론으로 강제 트리거됨)

telegram_listener.py / webhook_handler.py 가 등록한 data/holdings.csv 의 거래(trade_id)들을
감시하다가,
- 오늘 최고가가 이전 최고가를 갱신하면 -> 손절선도 "새 최고가 - 2xATR"로 같이 올림 (트레일링)
- 진입가 대비 ATR의 정수배(1배,2배,3배...)만큼 새로 오르면 -> "N배 수익 도달" 알림 (매도신호 아님)
- 오늘 저가가 손절선 밑으로 떨어지면 -> "트레일링 손절 도달" 알림 (매도 검토)
한 번 손절 알림 간 거래는 상태가 바뀌어서 더 이상 반복 알림이 안 갑니다.

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
  status 값: active(감시중) / stop_hit(손절도달) / closed_manual(수동청산)
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
from common import notify_telegram, send_long_message

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'holdings.csv')
COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'atr_entry',
           'highest_price', 'stop_price', 'last_milestone', 'status']
NUMERIC_COLUMNS = ['buy_price', 'atr_entry', 'highest_price', 'stop_price', 'last_milestone']
ATR_MULTIPLIER = 2

# 개장 전/마감 후 버퍼 (분 단위, 대칭)
PRE_MARKET_BUFFER_MIN = 5
POST_MARKET_BUFFER_MIN = 5


def fmt_num(v):
    v = float(v)
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
    if stop_price is None or stop_price == 0:
        return "🟢 정상"
    if current_price <= stop_price:
        return "⚠️ 손절선 이탈"
    gap_pct = (current_price - stop_price) / stop_price * 100
    if gap_pct <= 2.0:
        return "🔶 손절선 임박"
    return "🟢 정상"


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

    kr_open = is_korea_market_open()
    us_open = is_us_market_open()

    changed = False
    summary_lines = []

    for idx, row in df.iterrows():
        if row.get('status', 'active') != 'active':
            continue

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
        atr_entry = float(row['atr_entry'])
        highest_price = float(row['highest_price'])
        stop_price = float(row['stop_price'])
        last_milestone = int(row['last_milestone']) if pd.notna(row['last_milestone']) else 0

        # 1) 최고가 갱신 -> 트레일링 손절선 갱신 (내려가지는 않음)
        if ohlc['high'] > highest_price:
            highest_price = ohlc['high']
            new_stop = highest_price - ATR_MULTIPLIER * atr_entry
            if new_stop > stop_price:
                stop_price = new_stop
            df.at[idx, 'highest_price'] = float(highest_price)
            df.at[idx, 'stop_price'] = round(float(stop_price), 4)
            changed = True

        # 2) ATR 배수 마일스톤 체크 (매도 신호 아님, 진행 알림)
        if atr_entry > 0:
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
        stop_hit = ohlc['low'] <= stop_price
        if stop_hit:
            pnl_pct = (ohlc['close'] - buy_price) / buy_price * 100
            notify_telegram(
                f"[{market}] 트레일링 손절 도달! (매도 검토)\n"
                f"거래번호 {trade_id} - {code}\n"
                f"매수가 {fmt_num(buy_price)} / 최고가 {fmt_num(highest_price)} / "
                f"손절선 {fmt_num(stop_price)} / 현재가 {fmt_num(ohlc['close'])}\n"
                f"손익률 {pnl_pct:+.2f}%"
            )
            df.at[idx, 'status'] = 'stop_hit'
            changed = True
            print(f"거래 {trade_id}({code}) 트레일링 손절 도달 알림 전송")
        else:
            print(f"거래 {trade_id}({code}): 현재가 {ohlc['close']} "
                  f"(최고가 {highest_price} / 손절선 {stop_price}) - 감시 유지")

        # 4) 5분마다 무조건 포함되는 현재 상태 요약용 라인 (손절 도달로 방금
        # 종료된 거래는 요약에서 제외 - 위 알림으로 이미 충분히 안내됨)
        if not stop_hit:
            pnl_pct = (ohlc['close'] - buy_price) / buy_price * 100
            gap_to_stop_pct = (ohlc['close'] - stop_price) / stop_price * 100 if stop_price else 0
            atr_multiple = (highest_price - buy_price) / atr_entry if atr_entry > 0 else 0
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


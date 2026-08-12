# -*- coding: utf-8 -*-
"""보유종목 목표가/손절가 추적 (GitHub Actions에서 5분마다 자동 실행)

telegram_listener.py 가 처리한 data/holdings.csv 의 거래(trade_id)들을 감시하다가,
- 오늘 장중 고가가 목표가(target_price)를 넘으면 -> "목표가 도달" 알림
- 오늘 장중 저가가 손절가(stop_price) 밑으로 떨어지면 -> "손절가 도달" 알림
한 번 알림 간 거래는 상태가 바뀌어서 더 이상 반복 알림이 안 갑니다.

data/holdings.csv 컬럼: trade_id,market,code,buy_price,target_price,stop_price,status
  market 값: KR(국내) / US(미국) / COIN(빗썸)
  status 값: active(감시중) / target_hit(목표가도달) / stop_hit(손절가도달) / closed_manual(수동청산)
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
from common import notify_telegram

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'holdings.csv')
COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'target_price', 'stop_price', 'status']


def fmt_num(v):
    v = float(v)
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip('0').rstrip('.')


def is_korea_market_open():
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def is_us_market_open():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


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


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("data/holdings.csv 파일이 없어요. 텔레그램에서 buy 명령으로 먼저 등록해주세요.")
        sys.exit(0)

    df = pd.read_csv(DATA_PATH, dtype={'code': str, 'trade_id': str})
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ''
    df = df[COLUMNS]

    if df.empty:
        print("등록된 거래가 없습니다.")
        sys.exit(0)

    kr_open = is_korea_market_open()
    us_open = is_us_market_open()

    changed = False
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
        target_price = float(row['target_price']) if pd.notna(row.get('target_price')) and row.get('target_price') != '' else None
        stop_price = float(row['stop_price']) if pd.notna(row.get('stop_price')) and row.get('stop_price') != '' else None

        if target_price is not None and ohlc['high'] >= target_price:
            profit_pct = (ohlc['close'] - buy_price) / buy_price * 100
            notify_telegram(
                f"[{market}] 목표가 도달! (매도 검토)\n"
                f"거래번호 {trade_id} - {code}\n"
                f"매수가 {fmt_num(buy_price)} / 목표가 {fmt_num(target_price)} / 현재가 {fmt_num(ohlc['close'])}\n"
                f"수익률 {profit_pct:+.2f}%"
            )
            df.at[idx, 'status'] = 'target_hit'
            changed = True
            print(f"거래 {trade_id}({code}) 목표가 도달 알림 전송")

        elif stop_price is not None and ohlc['low'] <= stop_price:
            loss_pct = (ohlc['close'] - buy_price) / buy_price * 100
            notify_telegram(
                f"[{market}] 손절가 도달! (매도 검토)\n"
                f"거래번호 {trade_id} - {code}\n"
                f"매수가 {fmt_num(buy_price)} / 손절가 {fmt_num(stop_price)} / 현재가 {fmt_num(ohlc['close'])}\n"
                f"손익률 {loss_pct:+.2f}%"
            )
            df.at[idx, 'status'] = 'stop_hit'
            changed = True
            print(f"거래 {trade_id}({code}) 손절가 도달 알림 전송")
        else:
            print(f"거래 {trade_id}({code}): 현재가 {ohlc['close']} "
                  f"(목표 {target_price} / 손절 {stop_price}) - 감시 유지")

    if changed:
        df.to_csv(DATA_PATH, index=False)
        print("holdings.csv 상태 업데이트 완료")
    else:
        print("변경 사항 없음")
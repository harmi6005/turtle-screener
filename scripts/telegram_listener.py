# -*- coding: utf-8 -*-
"""텔레그램 채팅으로 buy/sell 명령만으로 보유종목을 추적하는 리스너
(holdings_check.py 실행 직전에 같이 돌면서, 사용자가 보낸 명령어를 처리합니다)

=== 사용법 (텔레그램 채팅창에 그대로 입력, 최대 5분 내로 처리됨) ===

매수 등록 (시장은 코드 형태로 자동 판별함 - 국내는 6자리 숫자, 나머지는
빗썸 코인인지 확인 후 아니면 미국 종목으로 처리):
  buy 코드 매수가
    예) buy BTC 5000000
        buy 005930 70000
        buy AAPL 220

  터틀 트레이딩 철학 그대로 "고정 목표가 없이, 오르는 동안은 최대한 들고 간다" 방식이에요.
  - 손절가는 진입 시점 ATR(최근 20일 변동성) 기준으로 "진입가 - 2xATR"로 시작하고,
    가격이 최고가를 경신할 때마다 "최고가 - 2xATR"로 계속 따라 올라가요 (트레일링 스탑).
    즉 오르면 손절선도 같이 올라가서 수익을 지켜주고, 절대 내려가지는 않아요.
  - 진입가 대비 ATR의 1배, 2배, 3배... 만큼 오를 때마다 "N배 수익 도달" 알림이 따로 와요
    (이건 매도 신호가 아니라 그냥 진행 상황 알림이에요).
  - 실제 매도 신호는 가격이 트레일링 손절선 아래로 떨어질 때만 옵니다.

청산 종료 (직접 팔았을 때):
  sell 거래번호 [매도가]
    예) sell 4821 6200000
        sell 4821

목록 조회:
  list
"""

import sys
import os
import random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta
from common import notify_telegram, calc_atr

HOLDINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'holdings.csv')
OFFSET_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'telegram_offset.txt')

COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'atr_entry',
           'highest_price', 'stop_price', 'last_milestone', 'status']
ATR_PERIOD = 20
ATR_MULTIPLIER = 2  # 터틀 오리지널 원칙: 손절폭 = 2 x ATR


def fmt_num(v):
    v = float(v)
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip('0').rstrip('.')


def get_updates(token, offset=None):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {'timeout': 5}
    if offset is not None:
        params['offset'] = offset
    res = requests.get(url, params=params, timeout=15).json()
    return res.get('result', [])


def load_offset():
    if os.path.exists(OFFSET_PATH):
        try:
            return int(open(OFFSET_PATH).read().strip())
        except Exception:
            return None
    return None


def save_offset(offset):
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    with open(OFFSET_PATH, 'w') as f:
        f.write(str(offset))


def load_holdings():
    if os.path.exists(HOLDINGS_PATH):
        df = pd.read_csv(HOLDINGS_PATH, dtype={'code': str, 'trade_id': str})
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ''
        return df[COLUMNS]
    return pd.DataFrame(columns=COLUMNS)


def save_holdings(df):
    os.makedirs(os.path.dirname(HOLDINGS_PATH), exist_ok=True)
    df.to_csv(HOLDINGS_PATH, index=False)


def detect_market(code):
    if code.isdigit() and len(code) == 6:
        return 'KR'
    try:
        url = f"https://api.bithumb.com/public/ticker/{code.upper()}_KRW"
        res = requests.get(url, timeout=5).json()
        if res.get('status') == '0000':
            return 'COIN'
    except Exception:
        pass
    return 'US'


def get_recent_ohlc(market, code, days=60):
    """ATR 계산에 필요한 최근 OHLC 데이터를 가져온다."""
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
            url = f"https://api.bithumb.com/public/candlestick/{code}_KRW/24h"
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
    except Exception:
        return None
    return None


def get_atr(market, code, period=ATR_PERIOD):
    df = get_recent_ohlc(market, code, days=period + 30)
    if df is None or len(df) < period + 1:
        return None
    atr_series = calc_atr(df, period)
    val = atr_series.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def gen_trade_id(df):
    existing = set(df['trade_id'].astype(str)) if not df.empty else set()
    while True:
        tid = f"{random.randint(0, 9999):04d}"
        if tid not in existing:
            return tid


def handle_buy(args, df):
    if len(args) < 2:
        return df, "형식: buy 코드 매수가\n예) buy BTC 5000000"

    code = args[0].upper()
    try:
        buy_price = float(args[1])
    except ValueError:
        return df, "매수가는 숫자로 입력해주세요."

    market = detect_market(code)

    atr = get_atr(market, code)
    if atr is None:
        return df, (f"{code}의 변동성(ATR) 데이터를 가져오지 못해서 등록에 실패했어요.\n"
                     f"종목 코드가 맞는지 확인해주세요 (시장 판별: {market}).")

    stop_price = buy_price - ATR_MULTIPLIER * atr
    if stop_price <= 0:
        stop_price = buy_price * 0.1

    trade_id = gen_trade_id(df)
    new_row = {'trade_id': trade_id, 'market': market, 'code': code,
               'buy_price': buy_price, 'atr_entry': round(atr, 6),
               'highest_price': buy_price, 'stop_price': round(stop_price, 4),
               'last_milestone': 0, 'status': 'active'}
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

    return df, (f"등록 완료 (거래번호 {trade_id})\n"
                f"{code} [{market}]\n"
                f"매수가 {fmt_num(buy_price)}\n"
                f"초기 손절가(진입가-2xATR) {fmt_num(stop_price)} (ATR≈{fmt_num(atr)})\n"
                f"목표가 없이 트레일링 방식으로 추적합니다. "
                f"오를 때마다 손절선도 같이 올라가고, ATR 배수 도달 시 알림 갈게요.")


def handle_sell(args, df):
    if not args:
        return df, "형식: sell 거래번호 [매도가]\n예) sell 4821 6200000"

    trade_id = args[0]
    sell_price = args[1] if len(args) > 1 else None

    mask = (df['trade_id'] == trade_id) & (df['status'] == 'active')
    if not mask.any():
        return df, f"거래번호 {trade_id}를 찾지 못했어요. list 로 확인해보세요."

    row = df[mask].iloc[0]
    extra = ""
    if sell_price:
        try:
            sp = float(sell_price)
            pnl = (sp - float(row['buy_price'])) / float(row['buy_price']) * 100
            extra = f"\n매도가 {fmt_num(sp)} / 손익률 {pnl:+.2f}%"
        except ValueError:
            extra = "\n(매도가 형식이 숫자가 아니라 손익률 계산은 생략했어요)"

    df.loc[mask, 'status'] = 'closed_manual'
    return df, f"청산 완료 (거래번호 {trade_id}): {row['code']} [{row['market']}]{extra}"


def handle_list(df):
    active = df[df['status'] == 'active']
    if active.empty:
        return "현재 감시 중인 거래가 없어요."
    lines = ["현재 감시 중인 거래:"]
    for _, r in active.iterrows():
        lines.append(f"[{r['trade_id']}] {r['code']} [{r['market']}] "
                      f"매수 {fmt_num(r['buy_price'])} / 최고가 {fmt_num(r['highest_price'])} / "
                      f"현재 손절선 {fmt_num(r['stop_price'])} / {r['last_milestone']}배 수익 도달")
    return "\n".join(lines)


if __name__ == "__main__":
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("텔레그램 설정이 없어서 명령어 처리를 건너뜁니다.")
        sys.exit(0)

    offset = load_offset()
    updates = get_updates(token, offset)
    if not updates:
        print("새 명령어 없음")
        sys.exit(0)

    df = load_holdings()
    last_id = offset

    for upd in updates:
        last_id = upd['update_id'] + 1
        msg = upd.get('message', {})
        text = msg.get('text', '').strip()
        if not text:
            continue

        parts = text.split()
        cmd = parts[0].lower().lstrip('/')
        args = parts[1:]

        reply = None
        if cmd == 'buy':
            df, reply = handle_buy(args, df)
        elif cmd == 'sell':
            df, reply = handle_sell(args, df)
        elif cmd == 'list':
            reply = handle_list(df)

        if reply:
            notify_telegram(reply)

    save_holdings(df)
    save_offset(last_id)
    print("명령어 처리 완료")
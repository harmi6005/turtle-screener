# -*- coding: utf-8 -*-
"""텔레그램 채팅으로 buy/sell 명령만으로 보유종목을 추적하는 리스너
(holdings_check.py 실행 직전에 같이 돌면서, 사용자가 보낸 명령어를 처리합니다)

=== 사용법 (텔레그램 채팅창에 그대로 입력, 최대 5분 내로 처리됨) ===

매수 등록 (시장은 코드 형태로 자동 판별함 - 국내는 6자리 숫자, 나머지는
빗썸 코인인지 확인 후 아니면 미국 종목으로 처리):
  buy 코드 매수가 목표가
    예) buy BTC 5000000 6000000
        buy 005930 70000 80000
        buy AAPL 220 250

  등록하면 4자리 거래번호를 자동 발급하고, 손절가도 자동 계산해서 같이 알려줘요.
  손절가 계산 방식: 터틀 트레이딩 오리지널 원칙 (진입가 - 2 x ATR)
    ATR(Average True Range)은 최근 20일 변동성을 의미하고, 실시간으로 조회해서 계산해요.
    즉 목표가와는 무관하게, 그 종목이 최근 실제로 얼마나 변동성이 컸는지로 손절폭을 정해요.

청산 종료:
  sell 거래번호 [매도가]
    예) sell 4821 6200000     (매도가 넣으면 손익률까지 계산)
        sell 4821             (매도가 생략 가능)

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

COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'target_price', 'stop_price', 'status']
ATR_PERIOD = 20
ATR_MULTIPLIER = 2  # 터틀 오리지널 원칙: 손절가 = 진입가 - 2 x ATR


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
    if len(args) < 3:
        return df, "형식: buy 코드 매수가 목표가\n예) buy BTC 5000000 6000000"

    code = args[0].upper()
    try:
        buy_price = float(args[1])
        target_price = float(args[2])
    except ValueError:
        return df, "매수가/목표가는 숫자로 입력해주세요."

    if target_price <= buy_price:
        return df, "목표가는 매수가보다 높아야 해요."

    market = detect_market(code)

    atr = get_atr(market, code)
    if atr is None:
        return df, (f"{code}의 변동성(ATR) 데이터를 가져오지 못해서 등록에 실패했어요.\n"
                     f"종목 코드가 맞는지 확인해주세요 (시장 판별: {market}).")

    stop_price = buy_price - ATR_MULTIPLIER * atr
    if stop_price <= 0:
        stop_price = buy_price * 0.1  # 극단적으로 음수/0이 되는 것 방지

    trade_id = gen_trade_id(df)
    new_row = {'trade_id': trade_id, 'market': market, 'code': code,
               'buy_price': buy_price, 'target_price': target_price,
               'stop_price': round(stop_price, 4), 'status': 'active'}
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

    return df, (f"등록 완료 (거래번호 {trade_id})\n"
                f"{code} [{market}]\n"
                f"매수가 {fmt_num(buy_price)} / 목표가 {fmt_num(target_price)}\n"
                f"손절가(터틀원칙, 진입가-2xATR) {fmt_num(stop_price)} "
                f"(ATR≈{fmt_num(atr)})")


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
                      f"매수 {fmt_num(r['buy_price'])} / 목표 {fmt_num(r['target_price'])} / "
                      f"손절 {fmt_num(r['stop_price'])}")
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
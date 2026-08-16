# -*- coding: utf-8 -*-
"""텔레그램 채팅으로 보유종목(buy/sell) 및 감시목록(watch/unwatch)을 관리하는 리스너
(holdings_check.py, watchlist_check.py 실행 직전에 같이 돌면서 명령어를 처리합니다)

=== 보유종목 (실제 매수한 것 추적) ===
  buy 코드 매수가
    예) buy BTC 5000000
    -> 4자리 거래번호 발급, 손절가는 터틀원칙(진입가-2xATR)으로 자동계산
       목표가 없이 트레일링 방식으로 추적 (오르면 손절선도 같이 올라감)

  sell 거래번호 [매도가]
    예) sell 4821 6200000

  list
    현재 감시 중인 보유거래 목록

=== 감시목록 (매수 여부와 상관없이 터틀 신호만 계속 지켜보고 싶은 종목) ===
  watch 코드   (또는 그냥 "코드 추적" 이라고 보내도 동일하게 작동)
    예) watch 005930
        005930 추적
    -> 가격범위 필터 등 상관없이, 이 종목만 따로 계속 감시.
       관심/진입/청산 신호가 바뀔 때마다 알림.

  unwatch 코드   (또는 "코드 추적해제")
    예) unwatch 005930
        005930 추적해제

  watchlist   (또는 "추적목록")
    현재 감시목록 확인
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
WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'watchlist.csv')
OFFSET_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'telegram_offset.txt')

HOLDINGS_COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'atr_entry',
                    'highest_price', 'stop_price', 'last_milestone', 'status']
WATCHLIST_COLUMNS = ['code', 'market', 'sys1_status', 'sys2_status']
ATR_PERIOD = 20
ATR_MULTIPLIER = 2


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


# ===== 보유종목(holdings) =====

def load_holdings():
    if os.path.exists(HOLDINGS_PATH):
        df = pd.read_csv(HOLDINGS_PATH, dtype={'code': str, 'trade_id': str})
        for col in HOLDINGS_COLUMNS:
            if col not in df.columns:
                df[col] = ''
        return df[HOLDINGS_COLUMNS]
    return pd.DataFrame(columns=HOLDINGS_COLUMNS)


def save_holdings(df):
    os.makedirs(os.path.dirname(HOLDINGS_PATH), exist_ok=True)
    df.to_csv(HOLDINGS_PATH, index=False)


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
                f"목표가 없이 트레일링 방식으로 추적합니다.")


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
                      f"손절선 {fmt_num(r['stop_price'])} / {r['last_milestone']}배 수익 도달")
    return "\n".join(lines)


# ===== 감시목록(watchlist) =====

def load_watchlist():
    if os.path.exists(WATCHLIST_PATH):
        df = pd.read_csv(WATCHLIST_PATH, dtype={'code': str})
        for col in WATCHLIST_COLUMNS:
            if col not in df.columns:
                df[col] = ''
        return df[WATCHLIST_COLUMNS]
    return pd.DataFrame(columns=WATCHLIST_COLUMNS)


def save_watchlist(df):
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    df.to_csv(WATCHLIST_PATH, index=False)


def handle_watch(args, wdf):
    if not args:
        return wdf, "형식: watch 코드\n예) watch 005930"
    code = args[0].upper()

    if (wdf['code'] == code).any():
        return wdf, f"{code}는 이미 감시 중이에요."

    market = detect_market(code)
    new_row = {'code': code, 'market': market, 'sys1_status': '', 'sys2_status': ''}
    wdf = pd.concat([wdf, pd.DataFrame([new_row])], ignore_index=True)
    return wdf, f"감시 등록 완료: {code} [{market}]\n관심/진입/청산 신호가 바뀔 때마다 알림 드릴게요."


def handle_unwatch(args, wdf):
    if not args:
        return wdf, "형식: unwatch 코드"
    code = args[0].upper()
    before = len(wdf)
    wdf = wdf[wdf['code'] != code]
    if len(wdf) == before:
        return wdf, f"{code}는 감시목록에 없어요."
    return wdf, f"{code} 감시를 해제했어요."


def handle_watchlist(wdf):
    if wdf.empty:
        return "현재 감시목록이 비어있어요."
    lines = ["현재 감시목록:"]
    for _, r in wdf.iterrows():
        lines.append(f"- {r['code']} [{r['market']}] "
                      f"(System1: {r['sys1_status'] or '-'} / System2: {r['sys2_status'] or '-'})")
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
    wdf = load_watchlist()
    last_id = offset
    holdings_changed = False
    watchlist_changed = False

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
            holdings_changed = True
        elif cmd == 'sell':
            df, reply = handle_sell(args, df)
            holdings_changed = True
        elif cmd == 'list':
            reply = handle_list(df)
        elif cmd == 'watch':
            wdf, reply = handle_watch(args, wdf)
            watchlist_changed = True
        elif cmd == 'unwatch':
            wdf, reply = handle_unwatch(args, wdf)
            watchlist_changed = True
        elif cmd == 'watchlist':
            reply = handle_watchlist(wdf)
        elif len(parts) == 2 and parts[1] == '추적':
            # "종목코드 추적" 형태의 자연어 명령 지원
            wdf, reply = handle_watch([parts[0]], wdf)
            watchlist_changed = True
        elif len(parts) == 2 and parts[1] in ('추적해제', '추적종료', '추적중지'):
            wdf, reply = handle_unwatch([parts[0]], wdf)
            watchlist_changed = True
        elif text.strip() in ('추적목록', '감시목록'):
            reply = handle_watchlist(wdf)

        if reply:
            notify_telegram(reply)

    if holdings_changed:
        save_holdings(df)
    if watchlist_changed:
        save_watchlist(wdf)
    save_offset(last_id)
    print("명령어 처리 완료")
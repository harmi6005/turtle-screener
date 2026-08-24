# -*- coding: utf-8 -*-
"""텔레그램 명령어 처리 공통 로직.
telegram_listener.py(폴링 방식, 5분마다)와 webhook_handler.py(웹훅 방식, 즉시)가
둘 다 이 모듈의 함수를 가져다 씁니다."""

import os
import random
import requests
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta
from common import SYSTEMS, WATCH_RATIO, calc_atr, check_turtle_breakout

HOLDINGS_PATH = os.path.join(os.path.dirname(__file__), 'data', 'holdings.csv')
WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), 'data', 'watchlist.csv')

HOLDINGS_COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'atr_entry',
                    'highest_price', 'stop_price', 'last_milestone', 'status']
WATCHLIST_COLUMNS = ['code', 'market', 'sys1_status', 'sys2_status']
ATR_PERIOD = 20
ATR_MULTIPLIER = 2

START_WORDS = ('추적시작',)
STOP_WORDS = ('추적종료', '추적해제', '추적중지')
CHECK_WORDS = ('추적확인', '추적목록')
HELP_WORDS = ('명령어확인', '명령어 확인', '명령어', '도움말', 'help', '/help')

HELP_TEXT = """사용 가능한 명령어 목록

[보유종목 (holdings)]
- buy 코드 매수가
  예) buy BTC 5000000
  시장 자동판별(KR 6자리숫자 / COIN 빗썸조회 / US 나머지)
  손절가 = 매수가 - 2xATR 자동계산, 4자리 거래번호 발급

- sell 거래번호 [매도가]
  예) sell 4821 6200000
  청산 처리. 매도가를 넣으면 손익률 자동계산

- list
  현재 보유 중인 거래 목록 (최고가/손절선/ATR배수 표시)
  ※ 5분마다 자동으로도 "[보유종목 현황]" 요약이 옵니다 (장중/코인 상시)

[집중추적종목 (watchlist)]
- 코드 추적시작
  예) 005930 추적시작
  매수 여부, 가격범위와 무관하게 특정 종목만 계속 감시

- 코드 추적종료  (추적해제 / 추적중지 도 동일)
  예) 005930 추적종료

- 추적확인  (추적목록 도 동일)
  지금 이 순간 실시간으로 재조회해서 현재 상태 보여줌
  ※ 5분마다 자동으로도 "[집중추적종목 현황]" 요약이 옵니다

[기타]
- 명령어 확인  (도움말 / help 도 동일)
  이 도움말 표시"""


def fmt_num(v):
    v = float(v)
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip('0').rstrip('.')


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
    """ATR / 터틀판정에 필요한 최근 OHLC 데이터를 가져온다."""
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

    # ⚠️ status == 'active'만 찾던 기존 로직 수정: 손절 확정(stop_hit) 상태인
    # 거래도 sell로 청산할 수 있어야 함. 이미 수동청산된(closed_manual) 거래만 제외.
    mask = (df['trade_id'] == trade_id) & (df['status'] != 'closed_manual')
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
    active = df[df['status'] != 'closed_manual']
    if active.empty:
        return "현재 감시 중인 거래가 없어요."
    lines = ["현재 감시 중인 거래:"]
    for _, r in active.iterrows():
        tag = " [손절확정/매도대기]" if r['status'] == 'stop_hit' else ""
        lines.append(f"[{r['trade_id']}] {r['code']} [{r['market']}]{tag} "
                      f"매수 {fmt_num(r['buy_price'])} / 최고가 {fmt_num(r['highest_price'])} / "
                      f"손절선 {fmt_num(r['stop_price'])} / {r['last_milestone']}배 수익 도달")
    return "\n".join(lines)


# ===== 감시목록(watchlist / 추적) =====

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


def handle_track_start(code, wdf):
    code = code.upper()
    if (wdf['code'] == code).any():
        return wdf, f"{code}는 이미 추적 중이에요."

    market = detect_market(code)
    new_row = {'code': code, 'market': market, 'sys1_status': '', 'sys2_status': ''}
    wdf = pd.concat([wdf, pd.DataFrame([new_row])], ignore_index=True)
    return wdf, f"추적시작: {code} [{market}]\n관심/진입/청산 신호가 바뀔 때마다 알림 드릴게요."


def handle_track_stop(code, wdf):
    code = code.upper()
    before = len(wdf)
    wdf = wdf[wdf['code'] != code]
    if len(wdf) == before:
        return wdf, f"{code}는 추적 중이 아니에요."
    return wdf, f"추적종료: {code}"


def handle_track_check(wdf):
    """지금 이 순간 실시간으로 재조회해서 현재 상태를 분석해 보여준다."""
    if wdf.empty:
        return "현재 추적 중인 종목이 없어요."

    lines = [f"추적 중인 종목 {len(wdf)}개 실시간 분석:"]
    for _, row in wdf.iterrows():
        code, market = row['code'], row['market']
        df = get_recent_ohlc(market, code, days=300)
        if df is None:
            lines.append(f"- {code} [{market}]: 데이터 조회 실패")
            continue

        lines.append(f"- {code} [{market}]")
        for sys_name, sysconf in SYSTEMS.items():
            res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
            if not res:
                lines.append(f"    {sys_name}: 데이터 부족")
                continue
            if res['entry_signal']:
                status = '진입'
            elif res['exit_signal']:
                status = '청산'
            elif res['watch_signal']:
                status = '관심'
            else:
                status = '관찰중'
            gap_pct = (res['close'] - res['n_high']) / res['n_high'] * 100
            lines.append(
                f"    {sys_name}: {status} | 현재가 {res['close']} / "
                f"N일고가 {res['n_high']} ({gap_pct:+.2f}%) / N일저가 {res['n_low']}"
            )
    return "\n".join(lines)


# ===== 도움말 =====

def handle_help():
    return HELP_TEXT


def dispatch(text, df, wdf):
    """명령어 텍스트 1개를 해석해서 처리한다.
    반환값: (df, wdf, reply_text_or_None, is_long_reply, holdings_changed, watchlist_changed)"""
    text = text.strip()
    if not text:
        return df, wdf, None, False, False, False

    parts = text.split()
    cmd = parts[0].lower().lstrip('/')
    args = parts[1:]

    reply = None
    is_long = False
    holdings_changed = False
    watchlist_changed = False

    if text in HELP_WORDS or cmd in HELP_WORDS:
        reply = handle_help()
        is_long = True
    elif cmd == 'buy':
        df, reply = handle_buy(args, df)
        holdings_changed = True
    elif cmd == 'sell':
        df, reply = handle_sell(args, df)
        holdings_changed = True
    elif cmd == 'list':
        reply = handle_list(df)
    elif len(parts) == 2 and parts[1] in START_WORDS:
        wdf, reply = handle_track_start(parts[0], wdf)
        watchlist_changed = True
    elif len(parts) == 2 and parts[1] in STOP_WORDS:
        wdf, reply = handle_track_stop(parts[0], wdf)
        watchlist_changed = True
    elif text in CHECK_WORDS:
        reply = handle_track_check(wdf)
        is_long = True

    return df, wdf, reply, is_long, holdings_changed, watchlist_changed


def dispatch_lines(text, df, wdf):
    """한 메시지에 명령어가 여러 줄로 들어있을 때(예: 'sell 8801\\nsell 7634') 줄
    단위로 각각 dispatch()에 넘겨서 순서대로 처리하고, 답장을 하나로 합쳐서
    반환한다. 한 줄짜리 메시지는 기존과 동일하게 동작한다."""
    lines = [ln for ln in text.split('\n') if ln.strip()]
    if len(lines) <= 1:
        return dispatch(text, df, wdf)

    replies = []
    is_long_any = False
    holdings_changed_any = False
    watchlist_changed_any = False

    for line in lines:
        df, wdf, reply, is_long, h_changed, w_changed = dispatch(line, df, wdf)
        if reply:
            replies.append(reply)
        is_long_any = is_long_any or is_long
        holdings_changed_any = holdings_changed_any or h_changed
        watchlist_changed_any = watchlist_changed_any or w_changed

    combined_reply = "\n\n".join(replies) if replies else None
    is_long_final = is_long_any or len(replies) > 1
    return df, wdf, combined_reply, is_long_final, holdings_changed_any, watchlist_changed_any

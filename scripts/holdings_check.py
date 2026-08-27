# -*- coding: utf-8 -*-
"""보유종목 트레일링 손절 + ATR 배수 수익 알림 + 5분 무조건 현황 요약
(GitHub Actions에서 5분마다 자동 실행)

v2.0 기준 전체 기능 반영판 (이전 배포본에서 build_status_tag() 정의 누락으로
NameError 발생하던 문제 수정 + 전체 재작성):

- 트레일링 손절선(최고가 - 2xATR) 자동 갱신, 절대 내려가지 않음
- ATR 정수 배수 마일스톤 최초 도달 시 1회 알림 (매도 신호 아님, 진행상황)
- 트레일링 손절선 이탈 시 1회 알림 (매도 검토) → 이후 status='stop_hit'
- status='stop_hit'이 되어도 sell 명령으로 수동 청산(status='closed_manual')하기
  전까지는 계속 요약/추적 대상에 포함됨 (반복 손절 알림만 안 감)
- 국장/미장은 개장 5분 전 ~ 마감 5분 후까지 "장중"으로 인식, 코인은 항상 체크
- 위 장중 조건에 해당하면 이변 여부와 무관하게 5분마다 전체 현황 요약 무조건 발송
  (📦 [보유종목 현황] N건 (5분 자동 갱신))
- atr_entry / stop_price가 NaN인 종목은 실행 시작 시 자동 재계산해서 채워 넣음
- fmt_num()은 NaN/None이 들어와도 죽지 않고 "N/A" 반환
- 매수가 대비 현재가 등락을 🔴▲ / 🔵▼ / 🟡➖ / 🆕 로 표시
- 트레일링 라인이 매수가를 처음 넘어서는 순간 "익절선 확정" 1회 알림
  (터틀 시스템엔 목표가가 없고, 이 라인이 손절선→익절선으로 성격이 바뀌는 게
  사실상의 "익절 기준"임)
- 트레일링 라인 이탈 시, 그 라인이 매수가 이상이면 "익절 도달", 미만이면
  "손절 도달"로 구분해서 알림
- 요약/이벤트 메시지에서 라인 이름을 상황에 맞게 "손절선"/"익절선"으로 자동 표시
- 요약에 1차/2차 익절 참고가(매수가+1xATR / +2xATR) 표시
  (⚠️ 실제 매도 트리거 아님. 터틀 시스템엔 목표가가 없고, 실제 매도 판단은
  위 트레일링 손절선/익절선 이탈 여부로만 함. 이건 어느 정도 왔는지 가늠하는
  참고용 기준선일 뿐)

data/holdings.csv 컬럼:
  trade_id,market,code,buy_price,atr_entry,highest_price,stop_price,
  last_milestone,status,last_price,breakeven_notified
  market 값: KR(국내) / US(미국) / COIN(빗썸)
  status 값: active(감시중) / stop_hit(손절확정, sell 대기) / closed_manual(수동청산)
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
from common import notify_telegram, send_long_message, calc_atr, fetch_with_retry

DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'holdings.csv')
COLUMNS = ['trade_id', 'market', 'code', 'buy_price', 'atr_entry',
           'highest_price', 'stop_price', 'last_milestone', 'status', 'last_price',
           'breakeven_notified']
NUMERIC_COLUMNS = ['buy_price', 'atr_entry', 'highest_price', 'stop_price',
                    'last_milestone', 'last_price']
ATR_MULTIPLIER = 2
PRE_MARKET_BUFFER_MIN = 5
POST_MARKET_BUFFER_MIN = 5


def fmt_num(v):
    """NaN/None이 들어와도 죽지 않고 'N/A'를 반환한다."""
    try:
        if v is None or pd.isna(v):
            return "N/A"
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip('0').rstrip('.')


def is_korea_market_open():
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    if now.weekday() >= 5:
        return False
    start = (datetime.combine(now.date(), dtime(9, 0)) - timedelta(minutes=PRE_MARKET_BUFFER_MIN)).time()
    end = (datetime.combine(now.date(), dtime(15, 30)) + timedelta(minutes=POST_MARKET_BUFFER_MIN)).time()
    return start <= now.time() <= end


def is_us_market_open():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    start = (datetime.combine(now.date(), dtime(9, 30)) - timedelta(minutes=PRE_MARKET_BUFFER_MIN)).time()
    end = (datetime.combine(now.date(), dtime(16, 0)) + timedelta(minutes=POST_MARKET_BUFFER_MIN)).time()
    return start <= now.time() <= end


def get_bithumb_daily_ohlc(coin, days=30):
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
            return {'high': float(last['High']), 'low': float(last['Low']), 'close': float(last['Close'])}

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
            return {'high': float(last['High']), 'low': float(last['Low']), 'close': float(last['Close'])}
    except Exception as e:
        print(f"  {code} 조회 실패: {e}")
        return None
    return None


def calc_atr_from_history(market, code, period=20):
    """atr_entry가 NaN인 보유종목을 위한 ATR 재계산."""
    try:
        if market == 'KR':
            end = datetime.today()
            start = end - timedelta(days=period + 40)
            df = fdr.DataReader(str(code).zfill(6), start, end)
        elif market == 'US':
            df = yf.download(code, period=f'{period + 40}d', auto_adjust=True, progress=False)
        elif market == 'COIN':
            df = get_bithumb_daily_ohlc(code, days=period + 40)
        else:
            return None
        if df is None or df.empty or len(df) < period + 1:
            return None
        atr_series = calc_atr(df, period)
        val = atr_series.iloc[-1]
        if pd.isna(val):
            return None
        return float(val)
    except Exception:
        return None


def get_kr_name_map(codes):
    """국장 보유종목의 코드->이름 매핑을 한 번에 조회한다.
    시장별 리스트 조회는 공용 재시도 헬퍼(fetch_with_retry)를 사용 (15초 간격 최대 3회).
    실패하거나 못 찾은 코드는 매핑에서 빠지고, 사용하는 쪽에서 코드만 표시하도록 fallback."""
    if not codes:
        return {}
    name_map = {}
    for market in ('KOSPI', 'KOSDAQ', 'KONEX'):
        listing = fetch_with_retry(
            lambda m=market: fdr.StockListing(m),
            retry_count=3, wait_sec=15, label=f"{market} 종목명 리스트",
        )
        if listing is None:
            continue  # 이 시장 이름 조회만 실패, 나머지는 계속 진행 (전체를 죽이지 않음)
        if 'Code' in listing.columns and 'Name' in listing.columns:
            for _, r in listing[['Code', 'Name']].iterrows():
                name_map[str(r['Code']).zfill(6)] = r['Name']
    return name_map


def build_line_label(stop_price, buy_price):
    """트레일링 라인이 지금 손절선 역할인지 익절선(손익확정) 역할인지 판정한다.
    터틀 시스템엔 별도 목표가가 없고, 이 라인이 매수가를 넘어서는 순간부터
    '더 이상 손해 볼 일 없는' 익절선으로 전환된다."""
    if pd.isna(stop_price) or pd.isna(buy_price):
        return "손절선"
    return "익절선" if stop_price >= buy_price else "손절선"


def build_status_tag(close, stop_price, buy_price=None):
    """라인까지 괴리율 기준으로 상태 태그를 부여한다.
    - 현재가 <= 라인: ⚠️ 손절선 이탈 / 🎯 익절선 이탈(라인이 매수가 이상이면)
    - 괴리율 <= 2.0%: 🔶 손절선 임박 / 🔶 익절선 임박
    - 그 외: 🟢 정상
    """
    try:
        if close is None or stop_price is None or pd.isna(close) or pd.isna(stop_price) or stop_price == 0:
            return "⬜ 상태미확인"
        label = build_line_label(stop_price, buy_price) if buy_price is not None else "손절선"
        if close <= stop_price:
            return "🎯 익절선 이탈" if label == "익절선" else "⚠️ 손절선 이탈"
        gap_pct = (close - stop_price) / stop_price * 100
        if gap_pct <= 2.0:
            return f"🔶 {label} 임박"
        return "🟢 정상"
    except Exception:
        return "⬜ 상태미확인"


def build_trend_tag(current, base):
    """기준가(매수가) 대비 등락 표시. 기준가가 없으면 🆕."""
    if base is None or pd.isna(base):
        return "🆕"
    try:
        diff = float(current) - float(base)
    except (TypeError, ValueError):
        return "🆕"
    if diff > 0:
        return f"🔴▲+{fmt_num(diff)}"
    elif diff < 0:
        return f"🔵▼{fmt_num(diff)}"
    return "🟡➖보합"


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("data/holdings.csv 파일이 없어요. 텔레그램에서 buy 명령으로 먼저 등록해주세요.")
        sys.exit(0)

    df = pd.read_csv(DATA_PATH, dtype={'code': str, 'trade_id': str})
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ''
    df = df[COLUMNS].copy()

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['last_milestone'] = df['last_milestone'].fillna(0)

    if df.empty:
        print("등록된 거래가 없습니다.")
        sys.exit(0)

    changed = False

    # NaN 자동 복구: atr_entry / stop_price 가 비어있으면 재계산해서 채워 넣는다.
    for idx, row in df.iterrows():
        if row.get('status') == 'closed_manual':
            continue
        if pd.isna(row['atr_entry']):
            recalced = calc_atr_from_history(row['market'], row['code'])
            if recalced is not None:
                df.at[idx, 'atr_entry'] = round(recalced, 6)
                changed = True
                print(f"거래 {row['trade_id']}({row['code']}) ATR 재계산 완료: {recalced}")
        atr_val = df.at[idx, 'atr_entry']
        if pd.isna(df.at[idx, 'stop_price']) and not pd.isna(atr_val) and not pd.isna(row['highest_price']):
            new_stop = float(row['highest_price']) - ATR_MULTIPLIER * float(atr_val)
            df.at[idx, 'stop_price'] = round(new_stop, 4)
            changed = True
            print(f"거래 {row['trade_id']}({row['code']}) 손절가 재계산 완료: {new_stop}")

    kr_open = is_korea_market_open()
    us_open = is_us_market_open()

    kr_codes = [
        str(r['code']).zfill(6) for _, r in df.iterrows()
        if r['market'] == 'KR' and r.get('status') != 'closed_manual'
    ]
    kr_name_map = get_kr_name_map(kr_codes)

    summary_rows = []

    for idx, row in df.iterrows():
        status = row.get('status', 'active')
        if status == 'closed_manual':
            continue  # 수동 청산된 거래만 감시 대상에서 완전 제외

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
        buy_price = row['buy_price']
        atr_entry = row['atr_entry']
        highest_price = row['highest_price'] if not pd.isna(row['highest_price']) else ohlc['high']
        stop_price = row['stop_price']
        last_milestone = int(row['last_milestone']) if pd.notna(row['last_milestone']) else 0
        last_price = row['last_price'] if not pd.isna(row['last_price']) else None
        breakeven_notified = str(row.get('breakeven_notified')) == 'True'
        stop_price_before_update = stop_price

        # active 상태에서만 트레일링/마일스톤/손절판정 진행
        # (stop_hit은 재판정 없이 감시만 계속 → 반복 손절알림 방지)
        if status == 'active':
            # 1) 최고가 갱신 -> 트레일링 손절선 갱신 (내려가지는 않음)
            if not pd.isna(atr_entry) and ohlc['high'] > highest_price:
                highest_price = ohlc['high']
                new_stop = highest_price - ATR_MULTIPLIER * atr_entry
                if pd.isna(stop_price) or new_stop > stop_price:
                    stop_price = new_stop
                df.at[idx, 'highest_price'] = float(highest_price)
                df.at[idx, 'stop_price'] = round(float(stop_price), 4)
                changed = True

            # 1-1) 트레일링 라인이 매수가를 처음 넘어서는 순간 = 익절선 확정 (1회 알림)
            # 터틀 시스템엔 목표가가 없고, 이 라인이 매수가 위로 올라오는 순간부터
            # "이탈해도 더 이상 손해가 아닌" 익절선으로 성격이 바뀐다.
            cur_stop_val = df.at[idx, 'stop_price']
            if (not breakeven_notified and not pd.isna(cur_stop_val) and not pd.isna(buy_price)
                    and (pd.isna(stop_price_before_update) or stop_price_before_update < buy_price)
                    and cur_stop_val >= buy_price):
                notify_telegram(
                    f"[{market}] 익절선 확정! (손익분기 돌파)\n"
                    f"거래번호 {trade_id} - {code}\n"
                    f"매수가 {fmt_num(buy_price)} / 현재 익절선(트레일링) {fmt_num(cur_stop_val)}\n"
                    f"이제부터 이 선을 이탈해도 손해가 아닌 이익 확정 매도가 됩니다."
                )
                df.at[idx, 'breakeven_notified'] = True
                breakeven_notified = True
                changed = True

            # 2) ATR 배수 마일스톤 체크 (정수배 최초 도달시 1회, 매도신호 아님)
            if not pd.isna(atr_entry) and atr_entry > 0 and not pd.isna(buy_price):
                current_multiple = int((df.at[idx, 'highest_price'] - buy_price) // atr_entry)
                if current_multiple > last_milestone:
                    profit_pct = (df.at[idx, 'highest_price'] - buy_price) / buy_price * 100
                    notify_telegram(
                        f"[{market}] {current_multiple}배 수익 도달 (진행상황)\n"
                        f"거래번호 {trade_id} - {code}\n"
                        f"매수가 {fmt_num(buy_price)} / 현재 최고가 {fmt_num(df.at[idx, 'highest_price'])}\n"
                        f"수익률 {profit_pct:+.2f}% / 현재 손절선 {fmt_num(df.at[idx, 'stop_price'])}"
                    )
                    df.at[idx, 'last_milestone'] = int(current_multiple)
                    last_milestone = current_multiple
                    changed = True
                    print(f"거래 {trade_id}({code}) {current_multiple}배 마일스톤 알림 전송")

            # 3) 트레일링 라인 이탈 체크 (진짜 매도 신호, 최초 1회만 알림)
            # 라인이 매수가보다 위에 있으면 이건 손절이 아니라 '익절 확정' 매도임
            cur_stop = df.at[idx, 'stop_price']
            if not pd.isna(cur_stop) and ohlc['low'] <= cur_stop:
                if not pd.isna(buy_price) and buy_price != 0:
                    pnl_txt = f"{(ohlc['close'] - buy_price) / buy_price * 100:+.2f}%"
                else:
                    pnl_txt = "N/A"
                line_label = build_line_label(cur_stop, buy_price)
                exit_kind = "익절 도달! (이익 확정 매도 검토)" if line_label == "익절선" else "트레일링 손절 도달! (매도 검토)"
                notify_telegram(
                    f"[{market}] {exit_kind}\n"
                    f"거래번호 {trade_id} - {code}\n"
                    f"매수가 {fmt_num(buy_price)} / 최고가 {fmt_num(df.at[idx, 'highest_price'])} / "
                    f"{line_label} {fmt_num(cur_stop)} / 현재가 {fmt_num(ohlc['close'])}\n"
                    f"손익률 {pnl_txt}"
                )
                df.at[idx, 'status'] = 'stop_hit'
                status = 'stop_hit'
                changed = True
                print(f"거래 {trade_id}({code}) 트레일링 손절 도달 알림 전송")

        # 요약용 정보 축적 (active / stop_hit 모두 포함)
        current_close = ohlc['close']
        trend_tag = build_trend_tag(current_close, buy_price)  # 매수가 기준 등락 표시
        cur_stop = df.at[idx, 'stop_price']
        line_label = build_line_label(cur_stop, buy_price)
        if status == 'stop_hit':
            tag = "🟢 익절 확정 (sell 명령 대기)" if line_label == "익절선" else "🔴 손절 확정 (sell 명령 대기)"
        else:
            tag = build_status_tag(current_close, cur_stop, buy_price)

        gap_pct = None
        if not pd.isna(cur_stop) and cur_stop != 0:
            gap_pct = (current_close - cur_stop) / cur_stop * 100

        pnl_pct = None
        if not pd.isna(buy_price) and buy_price != 0:
            pnl_pct = (current_close - buy_price) / buy_price * 100

        atr_multiple_now = None
        if not pd.isna(atr_entry) and atr_entry > 0 and not pd.isna(buy_price):
            atr_multiple_now = (df.at[idx, 'highest_price'] - buy_price) / atr_entry

        # 1차/2차 익절 참고가 (매수가 + 2xATR / 4xATR, 진입시점 ATR 고정 기준)
        # ⚠️ 실제 매도 트리거가 아니라 참고용 기준선. 실제 매도 판단은 트레일링
        # 손절선(익절선) 이탈 여부로만 한다.
        tp1 = tp2 = None
        if not pd.isna(atr_entry) and not pd.isna(buy_price):
            tp1 = buy_price + 2 * atr_entry
            tp2 = buy_price + 4 * atr_entry

        display_name = kr_name_map.get(str(code).zfill(6)) if market == 'KR' else None

        summary_rows.append({
            'trade_id': trade_id, 'market': market, 'code': code, 'name': display_name, 'tag': tag,
            'close': current_close, 'trend_tag': trend_tag,
            'buy_price': buy_price, 'pnl_pct': pnl_pct,
            'highest_price': df.at[idx, 'highest_price'], 'stop_price': cur_stop,
            'line_label': line_label,
            'gap_pct': gap_pct, 'atr_multiple_now': atr_multiple_now,
            'last_milestone': last_milestone, 'tp1': tp1, 'tp2': tp2,
        })

        df.at[idx, 'last_price'] = float(current_close)
        changed = True

        print(f"거래 {trade_id}({code}): 현재가 {current_close} "
              f"(최고가 {df.at[idx, 'highest_price']} / 손절선 {cur_stop}) - 감시 유지")

    # 5분마다 무조건 발송하는 보유종목 현황 요약
    if summary_rows:
        lines = [f"📦 [보유종목 현황] {len(summary_rows)}건 (5분 자동 갱신)"]
        for r in summary_rows:
            pnl_txt = f"{r['pnl_pct']:+.2f}%" if r['pnl_pct'] is not None else "N/A"
            gap_txt = f"{r['gap_pct']:+.2f}%" if r['gap_pct'] is not None else "N/A"
            atr_txt = f"{r['atr_multiple_now']:+.2f}배" if r['atr_multiple_now'] is not None else "N/A"
            code_display = f"{r['code']} {r['name']}" if r.get('name') else r['code']
            tp1_txt = fmt_num(r['tp1']) if r.get('tp1') is not None else "N/A"
            tp2_txt = fmt_num(r['tp2']) if r.get('tp2') is not None else "N/A"
            lines.append(
                f"- [{r['trade_id']}] {code_display} [{r['market']}] {r['tag']}\n"
                f"  현재가 {fmt_num(r['close'])} {r['trend_tag']} / 매수가 {fmt_num(r['buy_price'])} (손익 {pnl_txt})\n"
                f"  최고가 {fmt_num(r['highest_price'])} / {r['line_label']} {fmt_num(r['stop_price'])} (괴리율 {gap_txt})\n"
                f"  ATR배수 {atr_txt} (직전 마일스톤 {r['last_milestone']}배)\n"
                f"  1차 익절참고가(2×ATR) {tp1_txt} / 2차 익절참고가(4×ATR) {tp2_txt}"
            )
        send_long_message("\n".join(lines))

    if changed:
        df.to_csv(DATA_PATH, index=False)
        print("holdings.csv 상태 업데이트 완료")
    else:
        print("변경 사항 없음")

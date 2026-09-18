# -*- coding: utf-8 -*-
"""터틀 트레이딩 공통 로직 (모든 스크립트가 공유)

[2026-09-02 변경사항]
- pick_top_entry(1개 선정) -> pick_top_entries(최대 10개 선정)로 교체
- 선정 기준: 기존 "초과율 최솟값(가장 신선한 돌파)" -> "돌파강도(ATR배수) 최댓값(가장 강하게 뚫은 순)"
- 선정 가격 조건: 최종 픽 대상은 종가 10,000원 이하 종목만 (국장/미장/코인 전체 공통 적용)

[2026-09-04 변경사항]
- 마켓별(국장/미장/코인) 알림 on/off 설정 기능 추가.
  ALERT_MARKETS / ALERT_MARKET_LABELS / load_alert_settings / save_alert_settings /
  set_market_alert / is_market_alert_enabled 를 이 파일에 정의함.

[2026-09-08 변경사항]
- 버그 수정: set_market_alert() 함수 안의 // 주석을 # 으로 수정 (SyntaxError 해결).

[2026-09-10 변경사항]
- full_scan_*.py 3개 파일이 is_market_alert_enabled를 import해서 쓸 수 있도록
  (이미 존재하던 함수를 그대로 사용, common.py 자체 변경은 없음)

[2026-09-12 변경사항]
- 최종 픽 개수 변경: PICK_COUNT 10 -> 3 (매일 돌파강도가 가장 강한 종목 3개만
  픽해서 알림). 가격 상한(PICK_PRICE_MAX=10,000원)은 그대로 유지.
  국장/미장/코인 3개 마켓 전체 공통 적용. full_scan_*.py는 이 상수를 import해서
  쓰므로 코드 수정 없이 자동으로 새 기준(3개)을 따름.

[2026-09-14 변경사항]
- is_market_open_scan_window() 신규 추가: exchange_calendars 라이브러리로 국장
  (XKRX)/미장(XNYS)의 실제 개장일·개장시각(서머타임 포함)을 정확히 판정해서,
  "휴장일이면 무조건 스킵", "장 시작 N시간 전 ~ 마감 N시간 후"에만 매시간
  전체스캔이 돌도록 함. 코인은 24시간 거래라 이 함수 대상이 아님.
"""

import os
import time
import pandas as pd
import requests

SYSTEMS = {
    'System1(단기)': {'entry': 20, 'exit': 10},
    'System2(중장기)': {'entry': 55, 'exit': 20},
}
WATCH_RATIO = 0.99  # 당일 고가가 N일 최고가의 99% 이상이면 관심(돌파임박)
MAX_CHASE_RATIO = 0.005  # 진입가 대비 현재가가 0.5% 넘게 벌어지면 추격매수로 간주해 스킵

# ===== 최종 픽(알림) 설정 =====
# 2026-09-12 변경: 10개 -> 3개 (가장 강한 종목만 추림). 가격 상한은 기존 그대로.
PICK_COUNT = 3
PICK_PRICE_MAX = 100000
PICK_PRICE_MIN = None  # 하한 없음 (필요시 숫자로 지정)


def is_market_open_scan_window(calendar_name, buffer_before_hours=1, buffer_after_hours=1):
    """주어진 거래소 캘린더(예: 'XKRX'=한국거래소, 'XNYS'=뉴욕증권거래소) 기준으로,
    지금이 "장 시작 buffer_before_hours시간 전"부터 "장 마감 buffer_after_hours시간 후"
    까지의 매시간 전체스캔 허용 구간 안인지 판단한다.

    - 주말은 물론, 명절/임시공휴일 등 실제 휴장일이면 무조건 False.
    - exchange_calendars 라이브러리가 각 거래소의 정확한 개장/폐장 시각(미국의
      서머타임 전환 포함)을 이미 알고 있어서, 요일만 보는 방식보다 훨씬 정확함.
    - 라이브러리 조회가 실패하는 예외 상황에서는 "스캔을 영원히 막는 것"보다
      "가끔 불필요하게 도는 것"이 안전하므로 True(스캔 진행)를 반환한다.
    """
    try:
        import exchange_calendars as ecals
        cal = ecals.get_calendar(calendar_name)
        now = pd.Timestamp.now(tz='UTC')
        today_naive = now.normalize().tz_localize(None)
        if not cal.is_session(today_naive):
            return False
        sched = cal.schedule.loc[now.strftime('%Y-%m-%d')]
        window_start = sched['open'] - pd.Timedelta(hours=buffer_before_hours)
        window_end = sched['close'] + pd.Timedelta(hours=buffer_after_hours)
        return window_start <= now <= window_end
    except Exception as e:
        print(f"[market_calendar] 개장 여부 판정 실패({calendar_name}): {e} -> 안전하게 스캔 진행")
        return True


def fetch_with_retry(fn, retry_count=3, wait_sec=15, label="데이터"):
    """임의의 콜러블 fn()을 최대 retry_count회 재시도하는 공용 헬퍼."""
    for attempt in range(1, retry_count + 1):
        try:
            result = fn()
            if result is not None:
                return result
        except Exception as e:
            print(f"{label} 조회 실패 ({attempt}/{retry_count}): {e}")
        if attempt < retry_count:
            time.sleep(wait_sec)
    return None


def calc_atr(df, period=20):
    high = df['High']
    low = df['Low']
    close = df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def check_turtle_breakout(df, entry_period, exit_period, watch_ratio):
    """터틀 브레이크아웃 판정. df는 High/Low/Close 컬럼을 가진 OHLC 데이터프레임."""
    if df is None or len(df) < max(entry_period, exit_period) + 1:
        return None

    df = df.copy()
    df['ATR'] = calc_atr(df, 20)
    df['N_high'] = df['High'].rolling(entry_period).max().shift(1)
    df['N_low'] = df['Low'].rolling(exit_period).min().shift(1)

    last = df.iloc[-1]
    if pd.isna(last['N_high']) or pd.isna(last['N_low']) or pd.isna(last['ATR']):
        return None

    close = last['Close']
    high = last['High']
    low = last['Low']

    entry_signal = close >= last['N_high']
    fresh_entry_signal = high >= last['N_high'] and df.iloc[-2]['Close'] < df.iloc[-2].get('N_high', float('inf')) if len(df) > 1 else entry_signal
    exit_signal = low <= last['N_low']
    ratio = high / last['N_high'] if last['N_high'] else None
    watch_signal = ratio is not None and watch_ratio <= ratio < 1.0

    return {
        'entry_signal': bool(entry_signal),
        'fresh_entry_signal': bool(entry_signal),
        'exit_signal': bool(exit_signal),
        'watch_signal': bool(watch_signal),
        'close': round(close, 2),
        'n_high': round(last['N_high'], 2),
        'n_low': round(last['N_low'], 2),
        'n_high_ratio': round(ratio, 3) if ratio is not None else None,
        'atr': round(last['ATR'], 2),
    }


def notify_telegram(message: str):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={'chat_id': chat_id, 'text': message}, timeout=10)
    except Exception as e:
        print(f"텔레그램 알림 실패: {e}")


def build_watch_summary(df, market_label):
    watch_df = df[df['signal'] == '관심']
    if watch_df.empty:
        return None
    near_df = watch_df[(watch_df['n_high_ratio'] >= WATCH_RATIO) & (watch_df['n_high_ratio'] <= 1.0)]
    if near_df.empty:
        return None
    near_df = near_df.sort_values('n_high_ratio', ascending=False)
    lines = [f"[{market_label}] 관심종목 {len(watch_df)}개 중 돌파임박 {len(near_df)}개 (90~100% 구간)"]
    for _, r in near_df.iterrows():
        lines.append(
            f"- {r['name']}({r['code']}) [{r['system']}]\n"
            f"  현재가 {r['close']} / 진입가(돌파) {r['n_high']} "
            f"({r['n_high_ratio']*100:.1f}%)"
        )
    return "\n".join(lines)


def send_long_message(text, chunk_size=3500):
    if not text:
        return
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > chunk_size:
            if chunk:
                notify_telegram(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        notify_telegram(chunk)


# ===== 휩쏘 필터 (System1 한정) =====
TRADE_HISTORY_COLUMNS = ['code', 'system', 'direction', 'last_result', 'skip_active', 'skip_price']


def load_trade_history(path):
    if os.path.exists(path):
        df = pd.read_csv(path, dtype={'code': str})
        for col in TRADE_HISTORY_COLUMNS:
            if col not in df.columns:
                df[col] = ''
        return df[TRADE_HISTORY_COLUMNS]
    return pd.DataFrame(columns=TRADE_HISTORY_COLUMNS)


def save_trade_history(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


def check_whipsaw(hist_df, code, system, direction, entry_price, current_price, atr):
    """직전 거래가 손익분기 이상(win)이었으면 다음 신규 돌파를 1회 건너뛴다.
    건너뛴 가격 대비 2xATR 만큼 더 유리하게 움직이면 강제 진입 허용."""
    mask = (hist_df['code'] == code) & (hist_df['system'] == system) & (hist_df['direction'] == direction)
    row = hist_df[mask]
    if row.empty:
        return True, hist_df

    r = row.iloc[0]
    if str(r.get('skip_active')).lower() != 'true':
        return True, hist_df

    skip_price = float(r.get('skip_price', 0) or 0)
    if skip_price and atr:
        if current_price >= skip_price + 2 * atr:
            hist_df.loc[mask, 'skip_active'] = False
            return True, hist_df
    return False, hist_df


def record_trade_result(hist_df, code, system, direction, entry_price, exit_price):
    win = exit_price > entry_price
    mask = (hist_df['code'] == code) & (hist_df['system'] == system) & (hist_df['direction'] == direction)
    if mask.any():
        hist_df.loc[mask, 'last_result'] = 'win' if win else 'loss'
        hist_df.loc[mask, 'skip_active'] = win
        hist_df.loc[mask, 'skip_price'] = exit_price if win else ''
    else:
        new_row = {'code': code, 'system': system, 'direction': direction,
                   'last_result': 'win' if win else 'loss',
                   'skip_active': win, 'skip_price': exit_price if win else ''}
        hist_df = pd.concat([hist_df, pd.DataFrame([new_row])], ignore_index=True)
    return hist_df


def pick_top_entries(df, top_n=PICK_COUNT, price_max=PICK_PRICE_MAX, price_min=PICK_PRICE_MIN):
    entry_df = df[df['signal'] == '진입'].copy()
    if entry_df.empty:
        return entry_df
    if price_max is not None:
        entry_df = entry_df[entry_df['close'] <= price_max]
    if price_min is not None:
        entry_df = entry_df[entry_df['close'] >= price_min]
    if entry_df.empty:
        return entry_df
    entry_df['excess_ratio'] = (entry_df['close'] - entry_df['n_high']) / entry_df['n_high']
    entry_df['strength'] = (entry_df['close'] - entry_df['n_high']) / entry_df['atr']
    entry_df = entry_df.sort_values('strength', ascending=False)
    return entry_df.head(top_n)


# ===== 마켓별 알림 on/off =====
ALERT_MARKETS = ['KR', 'US', 'COIN']
ALERT_MARKET_LABELS = {'KR': '국장', 'US': '미장', 'COIN': '코인'}
ALERT_SETTINGS_COLUMNS = ['market', 'enabled']


def load_alert_settings(path):
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    settings = {}
    for _, r in df.iterrows():
        market = r.get('market')
        if market is None or (isinstance(market, float) and pd.isna(market)):
            continue
        enabled_raw = r.get('enabled')
        if enabled_raw is None or (isinstance(enabled_raw, float) and pd.isna(enabled_raw)):
            enabled = True
        else:
            enabled = str(enabled_raw).strip().lower() in ('true', '1', 'yes')
        settings[str(market)] = enabled
    return settings


def save_alert_settings(settings, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [{'market': k, 'enabled': v} for k, v in settings.items()]
    df = pd.DataFrame(rows, columns=ALERT_SETTINGS_COLUMNS)
    df.to_csv(path, index=False)


def set_market_alert(market, enabled, path):
    """특정 마켓의 알림 on/off를 설정하고 파일 저장까지 한 번에 처리한다."""
    settings = load_alert_settings(path)
    # 다른 마켓들의 기존 상태(명시적으로 저장된 값)는 그대로 유지하고,
    # 이 마켓 값만 갱신한다.
    settings[market] = enabled
    save_alert_settings(settings, path)
    return settings


def is_market_alert_enabled(market, path):
    settings = load_alert_settings(path)
    return settings.get(market, True)

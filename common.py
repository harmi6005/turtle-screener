# -*- coding: utf-8 -*-
"""터틀 트레이딩 공통 로직 (모든 스크립트가 공유)"""

import os
import pandas as pd
import requests

SYSTEMS = {
    'System1(단기)': {'entry': 20, 'exit': 10},
    'System2(중장기)': {'entry': 55, 'exit': 20},
}
WATCH_RATIO = 0.99  # 당일 고가가 N일 최고가의 99% 이상이면 관심(돌파임박)
MAX_CHASE_RATIO = 0.005  # 진입가 대비 현재가가 0.5% 넘게 벌어지면 추격매수로 간주해 스킵


def calc_atr(df, period=20):
    high, low, close = df['High'], df['Low'], df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def check_turtle_breakout(df, entry_period, exit_period, watch_ratio=0.9):
    if len(df) < entry_period + 5:
        return None
    df = df.copy()
    df['N_high'] = df['High'].rolling(entry_period).max().shift(1)
    df['N_low'] = df['Low'].rolling(exit_period).min().shift(1)
    df['ATR'] = calc_atr(df, 20)
    last = df.iloc[-1]
    entry_signal = last['Close'] > last['N_high']
    exit_signal = last['Close'] < last['N_low']
    ratio = last['High'] / last['N_high'] if last['N_high'] else None
    watch_signal = bool(ratio is not None and ratio >= watch_ratio and not entry_signal)
    return {
        'entry_signal': bool(entry_signal),
        'exit_signal': bool(exit_signal),
        'watch_signal': watch_signal,
        'close': round(last['Close'], 2),
        'high': round(last['High'], 2),
        'n_high': round(last['N_high'], 2),
        'n_low': round(last['N_low'], 2),
        'n_high_ratio': round(ratio, 3) if ratio is not None else None,
        'atr': round(last['ATR'], 2),
    }


def notify_telegram(message: str):
    """TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 깃허브 시크릿이 설정되어 있으면 알림 전송.
    설정 안 되어 있으면 조용히 건너뜀."""
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
    """관심종목 중 돌파(진입가)에 근접한 종목 전체를 진입가 포함해서 텔레그램 메시지로 정리.
    100%를 넘는 종목(장중 반짝 돌파 후 종가는 못 넘긴 케이스)은 제외하고,
    진짜 돌파 임박 종목만 보여줌. 개수 제한 없이 전부 표시."""
    watch_df = df[df['signal'] == '관심']
    if watch_df.empty:
        return None

    near_df = watch_df[(watch_df['n_high_ratio'] >= WATCH_RATIO) & (watch_df['n_high_ratio'] <= 1.0)]
    if near_df.empty:
        return None

    near_df = near_df.sort_values('n_high_ratio', ascending=False)
    lines = [f"[{market_label}] 관심종목 {len(watch_df)}개 중 돌파임박 {len(near_df)}개 "
             f"({WATCH_RATIO*100:.0f}~100% 구간)"]
    for _, r in near_df.iterrows():
        lines.append(
            f"- {r['name']}({r['code']}) [{r['system']}]\n"
            f"  현재가 {r['close']} / 진입가(돌파) {r['n_high']} "
            f"({r['n_high_ratio']*100:.1f}%)"
        )
    return "\n".join(lines)


def send_long_message(text, chunk_size=3500):
    """텔레그램 메시지 길이 제한(4096자)에 걸리지 않도록, 긴 텍스트를 여러 메시지로
    나눠서 순서대로 전송한다. 줄 단위로 잘라서 문장이 중간에 끊기지 않게 함."""
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
# 터틀 원칙: 직전 거래(같은 종목/같은 System1)가 수익이었다면 다음 신규 돌파 신호는
# 건너뛴다. 단, 건너뛴 진입가 대비 2xATR 만큼 더 유리한 방향으로 움직이면 그때는
# 필터를 무시하고 강제 진입한다. 직전 거래가 손절이었다면 필터 없이 정상 진입한다.

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


def _get_history_row(hist_df, code, system, direction):
    mask = (hist_df['code'] == code) & (hist_df['system'] == system) & (hist_df['direction'] == direction)
    if mask.any():
        return hist_df[mask].iloc[0], mask
    return None, mask


def check_whipsaw(hist_df, code, system, direction, breakout_price, current_price, atr):
    """System1 한정 휩쏘 필터. (진입 허용 여부, 갱신된 hist_df) 를 반환.
    direction: 'long' (지금은 롱만 사용, 숏은 추후 확장 예정)"""
    if system != 'System1(단기)':
        return True, hist_df  # System2는 이 필터 없음

    row, mask = _get_history_row(hist_df, code, system, direction)
    if row is None or row['last_result'] != 'win':
        return True, hist_df  # 직전이 손절이었거나 이력이 없으면 정상 진입

    skip_active = str(row.get('skip_active')) == 'True'
    if not skip_active:
        # 이번이 첫 스킵 -> 기록만 남기고 이번 신호는 건너뜀
        hist_df.loc[mask, 'skip_active'] = True
        hist_df.loc[mask, 'skip_price'] = breakout_price
        return False, hist_df

    skip_price = float(row['skip_price'])
    if direction == 'long':
        override = current_price >= skip_price + 2 * atr
    else:
        override = current_price <= skip_price - 2 * atr

    if override:
        hist_df.loc[mask, 'skip_active'] = False
        hist_df.loc[mask, 'skip_price'] = ''
        return True, hist_df
    return False, hist_df


def record_trade_result(hist_df, code, system, direction, entry_price, exit_price):
    """거래가 청산될 때 승/패를 이력에 기록하고, 스킵 상태는 초기화한다."""
    row, mask = _get_history_row(hist_df, code, system, direction)
    if direction == 'long':
        win = exit_price > entry_price
    else:
        win = exit_price < entry_price
    result = 'win' if win else 'loss'

    if mask.any():
        hist_df.loc[mask, 'last_result'] = result
        hist_df.loc[mask, 'skip_active'] = False
        hist_df.loc[mask, 'skip_price'] = ''
    else:
        new_row = {'code': code, 'system': system, 'direction': direction,
                   'last_result': result, 'skip_active': False, 'skip_price': ''}
        hist_df = pd.concat([hist_df, pd.DataFrame([new_row])], ignore_index=True)
    return hist_df
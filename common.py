# -*- coding: utf-8 -*-
"""터틀 트레이딩 공통 로직 (모든 스크립트가 공유)"""

import os
import pandas as pd
import requests

SYSTEMS = {
    'System1(단기)': {'entry': 20, 'exit': 10},
    'System2(중장기)': {'entry': 55, 'exit': 20},
}
WATCH_RATIO = 0.9
MAX_CHASE_RATIO = 0.01  # 진입가 대비 현재가가 1% 넘게 벌어지면 추격매수로 간주해 스킵


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

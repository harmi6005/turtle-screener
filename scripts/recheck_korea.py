# -*- coding: utf-8 -*-
"""국내 관심종목 재확인 (GitHub Actions에서 5분마다 자동 실행)
System1(단기)에는 터틀 휩쏘 필터가 적용됩니다 (직전 거래가 수익이었으면 다음
신규 돌파는 건너뛰고, 2xATR만큼 더 유리하게 움직이면 그때 강제 진입)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     load_trade_history, save_trade_history, check_whipsaw, record_trade_result)

MAX_WORKERS = 20
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')
HIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trade_history_korea.csv')


def is_korea_market_open():
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def recheck_one(row, start, end):
    code, name, system, orig_signal = row['code'], row['name'], row['system'], row['signal']
    sysconf = SYSTEMS.get(system)
    if not sysconf:
        return None
    try:
        df = fdr.DataReader(str(code).zfill(6), start, end)
        if df.empty:
            return {'code': code, 'name': name, 'system': system, 'status': '데이터없음'}
        res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
        if not res:
            return {'code': code, 'name': name, 'system': system, 'status': '데이터부족'}

        if orig_signal == '확정':
            status = '확정이탈' if res['exit_signal'] else '확정유지'
        elif res['entry_signal']:
            chase_ratio = (res['close'] - res['n_high']) / res['n_high']
            if chase_ratio > MAX_CHASE_RATIO:
                status = '스킵(추격과다)'
            else:
                status = '확정_candidate'  # 휩쏘 필터는 메인 스레드에서 순차 판정
        else:
            status = '유지' if res['watch_signal'] else '탈락'

        return {'code': code, 'name': name, 'system': system, 'status': status,
                'entry_price': row.get('entry_price', ''), **res}
    except Exception:
        return {'code': code, 'name': name, 'system': system, 'status': '오류'}


if __name__ == "__main__":
    if not is_korea_market_open():
        print("국내 장 시간이 아니라서 재확인을 건너뜁니다 (평일 09:00~15:30 KST).")
        sys.exit(0)

    if not os.path.exists(DATA_PATH):
        print("직전 결과 파일이 없어요. full_scan_korea.py를 먼저 실행해주세요.")
        sys.exit(0)

    prev_df = pd.read_csv(DATA_PATH)
    if 'entry_price' not in prev_df.columns:
        prev_df['entry_price'] = ''

    target_rows = prev_df[prev_df['signal'].isin(['관심', '확정'])].to_dict('records')
    print(f"관심/확정 종목 {len(target_rows)}개 재확인 중...")

    if not target_rows:
        print("현재 추적 중인 종목이 없습니다.")
        sys.exit(0)

    end = datetime.today()
    start = end - timedelta(days=180)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(recheck_one, row, start, end): row for row in target_rows}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    # 휩쏘 필터는 공유 이력 파일을 순차적으로 갱신해야 해서 메인 스레드에서 처리
    hist_df = load_trade_history(HIST_PATH)
    hist_changed = False
    confirm_rows = []
    exit_rows = []
    whipsaw_skip_count = 0

    for r in results:
        if r['status'] == '확정_candidate':
            allowed, hist_df = check_whipsaw(hist_df, r['code'], r['system'], 'long',
                                              r['n_high'], r['close'], r['atr'])
            hist_changed = True
            if allowed:
                r['status'] = '확정'
                confirm_rows.append(r)
            else:
                r['status'] = '관심'  # 이번엔 스킵, 계속 관심으로 유지
                whipsaw_skip_count += 1
        elif r['status'] == '확정이탈':
            exit_rows.append(r)

    result_df = pd.DataFrame(results)
    confirm_df = pd.DataFrame(confirm_rows)
    exit_df = pd.DataFrame(exit_rows)
    skip_df = result_df[result_df['status'] == '스킵(추격과다)']
    print(f"확정 {len(confirm_df)}개 / 확정이탈 {len(exit_df)}개 / "
          f"휩쏘스킵 {whipsaw_skip_count}개 / 스킵(추격과다) {len(skip_df)}개 / "
          f"유지 {len(result_df[result_df['status']=='유지'])}개 / "
          f"탈락 {len(result_df[result_df['status']=='탈락'])}개")

    for r in results:
        code, system, status = r['code'], r['system'], r['status']
        mask = (prev_df['code'] == code) & (prev_df['system'] == system)
        if status == '확정':
            prev_df.loc[mask, 'signal'] = '확정'
            prev_df.loc[mask, 'entry_price'] = r['close']
        elif status in ('탈락', '스킵(추격과다)'):
            prev_df.loc[mask, 'signal'] = '탈락'
        elif status == '확정이탈':
            prev_df.loc[mask, 'signal'] = '확정이탈'
            entry_price = r.get('entry_price', '')
            try:
                entry_price = float(entry_price)
                hist_df = record_trade_result(hist_df, code, system, 'long', entry_price, r['close'])
                hist_changed = True
            except (ValueError, TypeError):
                pass  # 옛날(휩쏘필터 도입 전) 거래는 entry_price가 없어서 이력 기록 생략

    prev_df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')
    if hist_changed:
        save_trade_history(hist_df, HIST_PATH)

    if not confirm_df.empty:
        lines = [f"- {r['name']}({r['code']}) [{r['system']}]\n"
                 f"  현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
                 f"  괴리율 {(r['close']-r['n_high'])/r['n_high']*100:.2f}%"
                 for _, r in confirm_df.iterrows()]
        notify_telegram("[국장] 확정 전환 종목! (매수 검토)\n" + "\n".join(lines))

    if not exit_df.empty:
        lines = [f"- {r['name']}({r['code']}) [{r['system']}]\n"
                 f"  현재가 {r['close']} / 청산가(손절) {r['n_low']}"
                 for _, r in exit_df.iterrows()]
        notify_telegram("[국장] 확정이탈 종목! (매도 검토)\n" + "\n".join(lines))
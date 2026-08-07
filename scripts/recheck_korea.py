# -*- coding: utf-8 -*-
"""국내 관심종목 재확인 (GitHub Actions에서 5분마다 자동 실행)"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import SYSTEMS, WATCH_RATIO, check_turtle_breakout, notify_telegram

MAX_WORKERS = 20
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')


def recheck_one(row, start, end):
    code, name, system = row['code'], row['name'], row['system']
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
        status = '확정' if res['entry_signal'] else ('유지' if res['watch_signal'] else '탈락')
        return {'code': code, 'name': name, 'system': system, 'status': status, **res}
    except Exception:
        return {'code': code, 'name': name, 'system': system, 'status': '오류'}


if __name__ == "__main__":
    if not os.path.exists(DATA_PATH):
        print("직전 결과 파일이 없어요. full_scan_korea.py를 먼저 실행해주세요.")
        sys.exit(0)

    prev_df = pd.read_csv(DATA_PATH)
    watch_rows = prev_df[prev_df['signal'] == '관심'].to_dict('records')
    print(f"관심종목 {len(watch_rows)}개 재확인 중...")

    if not watch_rows:
        print("현재 관심종목이 없습니다.")
        sys.exit(0)

    end = datetime.today()
    start = end - timedelta(days=180)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(recheck_one, row, start, end): row for row in watch_rows}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    result_df = pd.DataFrame(results)
    confirm_df = result_df[result_df['status'] == '확정']
    print(f"확정 {len(confirm_df)}개 / 유지 {len(result_df[result_df['status']=='유지'])}개 / "
          f"탈락 {len(result_df[result_df['status']=='탈락'])}개")

    for _, r in result_df.iterrows():
        mask = (prev_df['code'] == r['code']) & (prev_df['system'] == r['system'])
        if r['status'] == '확정':
            prev_df.loc[mask, 'signal'] = '확정'
        elif r['status'] == '탈락':
            prev_df.loc[mask, 'signal'] = '탈락'

    prev_df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')

    if not confirm_df.empty:
        lines = [f"- {r['name']}({r['code']}) [{r['system']}] 종가 {r['close']}"
                 for _, r in confirm_df.iterrows()]
        notify_telegram("[국내] 확정 전환 종목!\n" + "\n".join(lines))

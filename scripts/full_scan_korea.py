# -*- coding: utf-8 -*-
"""국내 주식 전체 스캔 (GitHub Actions에서 지정 시간에 자동 실행)

[2026-09-02 변경사항]
- 기존: 스캔 대상 자체를 종가 20,000~60,000원 종목으로 좁혀서 진행
- 변경: 스캔 대상은 코스피(KOSPI) 전체로 확대.

[2026-09-10 1차 수정] "국장알림중지"가 전체스캔에는 반영이 안 되던 버그 수정
[2026-09-10 2차 수정] KRX 종목 리스트 조회에 pykrx/네이버/캐시 폴백 추가
[2026-09-10 3차 수정] 한국투자증권 공식 종목마스터 파일을 종목 리스트 최우선 소스로 추가

[2026-09-10 4차 수정 - 전체 교체]
- ⭐ 사용자가 한국투자증권 오픈API(KIS Developers) App Key/App Secret을 실제로 발급받아
  연동을 요청함. 이번 수정부터는 "종목 리스트"뿐 아니라 "가격(일봉) 데이터"도 KIS 공식
  인증 API로 우선 조회하도록 확장함 (kis_client.py 신규 모듈).

  개별 종목의 300일치 일봉 조회 우선순위:
  ① KIS 인증 API (kis_client.get_kis_daily_ohlc) - KIS_APP_KEY/KIS_APP_SECRET
     환경변수(GitHub Secrets)가 설정되어 있을 때만 시도. 초당 호출 제한을 지키기 위해
     내부적으로 초당 약 10회로 자동 제한됨.
  ② FinanceDataReader (기존 방식) - KIS 자격증명이 없거나, 있어도 실패했을 때 폴백.

  종목 리스트 조회는 기존 5중 폴백(KIS 마스터파일 -> fdr -> pykrx -> 네이버 -> 캐시) 그대로 유지.

  ⚠️ 참고: KIS_APP_KEY/KIS_APP_SECRET이 GitHub Secrets에 없으면 이 스크립트는 자동으로
  기존 fdr 방식으로만 동작합니다 (에러 없이 조용히 폴백). 즉 이 커밋을 올려도 Secrets를
  등록하기 전까지는 동작이 그대로입니다.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import io
import time
import zipfile
import pandas as pd
import requests
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     build_watch_summary, send_long_message, pick_top_entries,
                     PICK_COUNT, PICK_PRICE_MAX, PICK_PRICE_MIN,
                     is_market_alert_enabled)
from kis_client import kis_credentials_available, get_kis_daily_ohlc

MAX_WORKERS = 20
MARKET = 'KOSPI'
MARKET_KEY = 'KR'
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')
ALERT_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'alert_settings.csv')
TICKER_CACHE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'kospi_tickers_cache.csv')

KRX_RETRY_COUNT = 5
KRX_RETRY_WAIT_SEC = 20
KIS_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"

KIS_AVAILABLE = kis_credentials_available()


def fetch_ohlc(code, start, end):
    """① KIS 인증 API -> ② fdr 순으로 개별 종목 일봉 데이터를 가져온다."""
    if KIS_AVAILABLE:
        df = get_kis_daily_ohlc(code, days=300)
        if df is not None and not df.empty and len(df) >= 60:
            return df
    try:
        df = fdr.DataReader(code, start, end)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return None


def fetch_and_check(code_name, start, end):
    code, name = code_name
    df = fetch_ohlc(code, start, end)
    if df is None or df.empty or len(df) < 60:
        return []

    rows = []
    for sys_name, sysconf in SYSTEMS.items():
        res = check_turtle_breakout(df, sysconf['entry'], sysconf['exit'], WATCH_RATIO)
        if not res:
            continue
        if res['fresh_entry_signal']:
            chase_ratio = (res['close'] - res['n_high']) / res['n_high']
            if chase_ratio > MAX_CHASE_RATIO:
                continue  # 이미 너무 많이 오른 상태 -> 진입 후보에서 제외
            signal = '진입'
        elif res['exit_signal']:
            signal = '청산'
        elif res['watch_signal']:
            signal = '관심'
        else:
            continue
        rows.append({'code': code, 'name': name, 'system': sys_name, 'signal': signal, **res})
    return rows


def get_kospi_listing_kis_master():
    """⓪ 한국투자증권 공식 종목마스터 파일. API 키 불필요, KRX/네이버와 무관한 서버."""
    try:
        res = requests.get(KIS_MASTER_URL, timeout=15)
        res.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            mst_names = [n for n in zf.namelist() if n.lower().endswith('.mst')]
            if not mst_names:
                print("[kis마스터] zip 안에 .mst 파일이 없습니다.")
                return None
            raw_bytes = zf.read(mst_names[0])
        raw_text = raw_bytes.decode('cp949', errors='ignore')
    except Exception as e:
        print(f"[kis마스터] 다운로드/압축해제 실패: {e}")
        return None

    rows = []
    for line in raw_text.splitlines():
        if len(line) <= 228:
            continue
        head = line[:-228]
        code = head[0:9].strip()
        name = head[21:].strip()
        if code and name:
            rows.append([code, name])

    if not rows:
        print("[kis마스터] 파싱 결과가 비어있습니다 (파일 형식이 바뀌었을 수 있음).")
        return None
    return pd.DataFrame(rows, columns=['Code', 'Name']).drop_duplicates(subset=['Code'])


def get_kospi_listing_fdr():
    """① FinanceDataReader (내부적으로 KRX 데이터를 씀)."""
    for attempt in range(1, KRX_RETRY_COUNT + 1):
        try:
            listing = fdr.StockListing(MARKET)
            if listing is not None and not listing.empty:
                return listing[['Code', 'Name']]
        except Exception as e:
            print(f"[fdr] KOSPI 목록 조회 실패 ({attempt}/{KRX_RETRY_COUNT}): {e}")
        if attempt < KRX_RETRY_COUNT:
            time.sleep(KRX_RETRY_WAIT_SEC)
    return None


def get_kospi_listing_pykrx():
    """② pykrx (이것도 결국 data.krx.co.kr을 씀 - fdr과 같은 이유로 같이 막힐 수 있음)."""
    try:
        from pykrx import stock
    except ImportError:
        print("[pykrx] 패키지가 설치되어 있지 않아 폴백을 건너뜁니다 (requirements.txt에 pykrx 추가 필요).")
        return None

    today = datetime.today().strftime('%Y%m%d')
    for attempt in range(1, KRX_RETRY_COUNT + 1):
        try:
            codes = stock.get_market_ticker_list(today, market=MARKET)
            if codes:
                rows = []
                for code in codes:
                    try:
                        name = stock.get_market_ticker_name(code)
                    except Exception:
                        name = code
                    rows.append([code, name])
                if rows:
                    return pd.DataFrame(rows, columns=['Code', 'Name'])
        except Exception as e:
            print(f"[pykrx] KOSPI 목록 조회 실패 ({attempt}/{KRX_RETRY_COUNT}): {e}")
        if attempt < KRX_RETRY_COUNT:
            time.sleep(KRX_RETRY_WAIT_SEC)
    return None


def get_kospi_listing_naver():
    """③ 네이버 금융 모바일 API."""
    rows = []
    page = 1
    max_pages = 20
    try:
        while page <= max_pages:
            url = f"https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page={page}&pageSize=100"
            res = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if res.status_code != 200:
                break
            data = res.json()
            stocks = data.get('stocks', [])
            if not stocks:
                break
            for s in stocks:
                code = s.get('itemCode') or s.get('code')
                name = s.get('stockName') or s.get('itemName') or s.get('name')
                if code and name:
                    rows.append([code, name])
            total_count = data.get('totalCount', 0)
            if len(rows) >= total_count or len(stocks) < 100:
                break
            page += 1
            time.sleep(0.3)
    except Exception as e:
        print(f"[naver] KOSPI 목록 조회 실패: {e}")
        return None

    if not rows:
        return None
    return pd.DataFrame(rows, columns=['Code', 'Name']).drop_duplicates(subset=['Code'])


def get_kospi_listing_cache():
    """④ 최후의 수단: 직전에 성공했던 목록을 저장소에서 그대로 읽음."""
    if not os.path.exists(TICKER_CACHE_PATH):
        print("[cache] 캐시된 종목 리스트 파일이 없습니다.")
        return None
    try:
        df = pd.read_csv(TICKER_CACHE_PATH, dtype={'Code': str})
        if df is not None and not df.empty and 'Code' in df.columns and 'Name' in df.columns:
            print(f"[cache] 캐시된 종목 리스트 {len(df)}개 사용 (최신 상장/상장폐지가 반영 안 됐을 수 있음)")
            return df[['Code', 'Name']]
    except Exception as e:
        print(f"[cache] 캐시 파일 읽기 실패: {e}")
    return None


def save_kospi_listing_cache(listing):
    try:
        os.makedirs(os.path.dirname(TICKER_CACHE_PATH), exist_ok=True)
        listing.to_csv(TICKER_CACHE_PATH, index=False, encoding='utf-8-sig')
        print(f"[cache] 종목 리스트 {len(listing)}개 캐시에 저장")
    except Exception as e:
        print(f"[cache] 캐시 저장 실패: {e}")


def get_kospi_listing():
    for label, fn in (('kis마스터', get_kospi_listing_kis_master),
                       ('fdr', get_kospi_listing_fdr),
                       ('pykrx', get_kospi_listing_pykrx),
                       ('naver', get_kospi_listing_naver)):
        listing = fn()
        if listing is not None and not listing.empty:
            print(f"[국장] {label} 소스로 종목 리스트 조회 성공 ({len(listing)}개)")
            save_kospi_listing_cache(listing)
            return listing
        print(f"[국장] {label} 소스 실패 -> 다음 소스 시도")

    print("[국장] kis마스터/fdr/pykrx/naver 모두 실패 -> 캐시된 마지막 성공 리스트로 폴백")
    return get_kospi_listing_cache()


def screen_korea():
    print(f"[국장] {MARKET} 종목 리스트 불러오는 중...")
    if KIS_AVAILABLE:
        print("[국장] KIS_APP_KEY/KIS_APP_SECRET 감지됨 - 가격 조회는 KIS 인증 API를 우선 사용합니다.")
    else:
        print("[국장] KIS 자격증명이 없어 가격 조회는 기존 fdr 방식으로 진행합니다.")

    listing = get_kospi_listing()
    if listing is None:
        return None

    tickers = listing[['Code', 'Name']].values.tolist()
    print(f"총 {len(tickers)}개 종목 병렬 조회 시작 (가격필터 없이 코스피 전체 스캔)")

    end = datetime.today()
    start = end - timedelta(days=300)

    # KIS API는 자체적으로 초당 호출 제한이 걸려 있어 스레드 수가 많아도 안전하지만,
    # fdr 폴백 비중이 크다면 기존과 동일하게 MAX_WORKERS로 병렬 처리한다.
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_and_check, cn, start, end): cn for cn in tickers}
        for future in as_completed(futures):
            done += 1
            rows = future.result()
            if rows:
                results.extend(rows)
            if done % 100 == 0:
                print(f"  ...{done}/{len(tickers)} 완료")

    return pd.DataFrame(results)


def build_pick_message(entry_cnt, top_df):
    lines = [f"[국장 전체스캔] 진입 신호 {entry_cnt}개 중 {PICK_PRICE_MAX:,}원 이하 "
             f"돌파강도 상위 {len(top_df)}픽 (최대 {PICK_COUNT}픽 중 {len(top_df)}개)"]
    for i, (_, r) in enumerate(top_df.iterrows(), 1):
        lines.append(
            f"{i}. {r['name']}({r['code']}) [{r['system']}]\n"
            f"   현재가 {r['close']} / 진입가(돌파) {r['n_high']} / 청산가(손절) {r['n_low']}\n"
            f"   돌파강도(ATR배수) {r['strength']:.2f} / 초과율 {r['excess_ratio']*100:.3f}%"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    alerts_enabled = is_market_alert_enabled(MARKET_KEY, ALERT_SETTINGS_PATH)
    if not alerts_enabled:
        print("[국장] 알림 중지 상태입니다 - 스캔/저장은 정상 진행하되 텔레그램 발송만 생략합니다.")

    def notify(msg):
        if alerts_enabled:
            notify_telegram(msg)

    def notify_long(text):
        if alerts_enabled:
            send_long_message(text)

    df = screen_korea()

    if df is None:
        notify("[국장 전체스캔] 스캔 실패 - KOSPI 종목 리스트를 불러오지 못했습니다 "
               "(KRX 서버 오류로 추정, kis마스터/fdr/pykrx/네이버/캐시 모두 실패).")
        sys.exit(0)

    print(f"\n[국장] 신호 종목 {len(df)}개 발견")

    if os.path.exists(DATA_PATH):
        prev_df = pd.read_csv(DATA_PATH, dtype={'code': str})
        confirmed_prev = prev_df[prev_df['signal'] == '확정']
        if not confirmed_prev.empty:
            new_keys = set(zip(df['code'], df['system'])) if not df.empty else set()
            keep_rows = confirmed_prev[~confirmed_prev.apply(
                lambda r: (r['code'], r['system']) in new_keys, axis=1)]
            if not keep_rows.empty:
                df = pd.concat([df, keep_rows], ignore_index=True)
                print(f"기존 확정 종목 {len(keep_rows)}개 보존")

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')
    print(f"결과 저장: {DATA_PATH}")

    entry_cnt = len(df[df['signal'] == '진입']) if not df.empty else 0
    watch_cnt = len(df[df['signal'] == '관심']) if not df.empty else 0

    if entry_cnt > 0:
        entry_only_df = df[df['signal'] == '진입']
        price_ok_cnt = len(entry_only_df[entry_only_df['close'] <= PICK_PRICE_MAX]) if PICK_PRICE_MAX is not None else entry_cnt
        print(f"[국장] 진입신호 {entry_cnt}개 중 {PICK_PRICE_MAX:,}원 이하 {price_ok_cnt}개 "
              f"(이 중 최대 {PICK_COUNT}개까지 알림)")

        top_df = pick_top_entries(df, top_n=PICK_COUNT, price_max=PICK_PRICE_MAX, price_min=PICK_PRICE_MIN)
        if not top_df.empty:
            notify_long(build_pick_message(entry_cnt, top_df))
        else:
            notify(f"[국장 전체스캔] 진입 신호 {entry_cnt}개가 있지만 "
                   f"{PICK_PRICE_MAX:,}원 이하 조건을 만족하는 종목이 없습니다.")
    else:
        notify("[국장 전체스캔] 실행 완료 - 부합 종목 없음")

    if watch_cnt > 0:
        summary = build_watch_summary(df, "국장")
        if summary:
            notify_long(summary)
    else:
        notify("[국장 전체스캔] 관심종목 없음")

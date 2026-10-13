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

[2026-09-12 5차 수정 - 전체 교체]
- ⭐ 검색 범위를 코스피(KOSPI) 단일 시장에서 **코스피+코스닥(KOSDAQ) 통합**으로 확대.
  MARKET(문자열) 상수를 MARKETS(리스트) 상수로 바꾸고, 종목 리스트 조회 함수
  4종(kis마스터/fdr/pykrx/naver)이 전부 두 시장을 각각 조회한 뒤 하나로 합치도록 수정.
  KIS 마스터 파일도 코스피용(kospi_code.mst.zip)과 코스닥용(kosdaq_code.mst.zip)
  두 개를 모두 받아서 합침. 가격(일봉) 조회 로직(fetch_ohlc, KIS/fdr 우선순위)과
  최종 픽 기준(PICK_COUNT=3, PICK_PRICE_MAX=30000)은 변경 없음 - 스캔 "대상 종목 수"만
  늘어난 것이므로, 코스닥 종목도 동일한 터틀 로직/픽 기준을 그대로 적용받음.

[2026-09-20 6차 수정 - 전체 교체]
- ⭐ 주말(토/일, KST 기준)에는 국장이 쉬는데도 전체스캔 알림이 계속 오던 문제 수정.
  원인: 워크플로우 cron이 요일 구분 없이 매일 실행되도록 되어 있었고, 스크립트 쪽에도
  휴장일 체크가 전혀 없었음.
  수정: ① 워크플로우 cron을 월~금(1-5)으로 제한 (full_scan_korea.yml, 별도 교체)
        ② 스크립트 맨 앞에서 휴장일이면 스캔/저장/알림 전부 생략하고 종료 (이중 방어)
        ③ 수동 실행(workflow_dispatch)은 휴장일에도 정상 동작 - 테스트/점검용

[2026-09-24 7차 수정 - 전체 교체]
- 🐛 위 6차 수정은 "주말"만 걸렀을 뿐 설/추석 같은 평일 공휴일은 걸러내지 못해서,
  공휴일에도 스케줄 실행(cron은 월~금이라 그대로 돎)되면 알림이 계속 갔음.
  common.py에 새로 추가된 is_korea_trading_day() (exchange_calendars로 KRX
  실제 개장일을 정확히 판정 - 주말+공휴일 모두 포함)로 교체하고, 기존 자체
  구현이었던 is_weekend_kst()는 제거함. requirements.txt에 exchange_calendars가
  추가되어 있어야 정상 동작하며(누락 시 주말 여부만으로 안전하게 폴백).

[2026-10-09 16차 수정 - 전체 교체]
- ⭐ 사용자 요청: 알림은 "확정 전환 종목"만 받기로 함. 전체스캔이 보내던 "진입 신호 상위 3픽",
  "실행 완료 - 부합 종목 없음", "관심종목 요약/없음" 알림을 전부 제거함(콘솔 로그만 남김).
  스캔 결과(data/turtle_korea_result.csv)에 '진입' 신호로 기록된 종목은 recheck_korea.py가
  5분마다 재확인(추격/휩쏘 필터)해서 통과하면 "확정 전환" 알림을 1회 보냄.
  (스캔 실패 알림만은 무음 장애를 막기 위해 유지)
- 실행 주기는 평일 30분마다(full_scan_korea.yml).
- 국장전체스캔중지/시작 명령(scan_settings.csv)이 국장에도 적용되도록 체크 추가.

[2026-10-13 17차 수정 - 전체 교체]
- ⭐ 사용자 요청(종목선정 점검 후 수정): 스캔 대상에서 ETF / ETN / 스팩을 제외함.
  배경: 코스피+코스닥 2,438개 중 ETF가 약 791개(32%), 스팩 29개가 섞여 있었고, 저장소의
  '확정' 140종목 중 125개가 ETF/스팩이었음(대부분 머니마켓·채권형 ETF로 ATR 평균 0.23% -
  이자로 천천히 오르는 것을 "돌파"로 잡고 있었음). 일반 주식은 15개뿐이었음.
  원본 터틀은 유동성 좋은 추세형 시장만 거래하므로 이 대상들은 종목선정 취지와 맞지 않음.
- 제외 규칙(이름 기준, 2,438개 종목명으로 오탐/미탐 검증 완료):
  ① 이름에 '스팩' 포함  ② 이름에 'ETN' 포함
  ③ 이름이 "ETF 브랜드 + 공백"으로 시작 (KODEX/TIGER/RISE/ACE/PLUS/SOL/KIWOOM/HANARO/1Q/
     KoAct/TIME/WON/BNK/HK/IBK/MIDAS/FOCUS/UNICORN/TREX/TRUSTON/DAISHIN/파워/마이티/에셋플러스 등)
     -> '파워넷', 'HK이노엔', 'BNK금융지주', '유진투자증권'처럼 공백 없이 붙은 일반 종목은 유지
  ④ 이름이 '액티브' 또는 'X클래스'로 끝남 (브랜드 목록에 없는 ETF 대비)
- 제외 대상은 가격 조회 전에 걸러내므로 조회 대상이 약 35% 줄어 스캔도 빨라짐.
- 직전 결과에서 보존하던 '확정' 종목에도 같은 규칙을 적용해 기존 ETF/스팩 확정 종목을 정리함
  (다음 스캔부터 data/turtle_korea_result.csv에서 사라짐).
- 안전장치: 제외 비율이 60%를 넘으면 규칙 오작동으로 보고 필터 없이 진행(로그에 경고).
- 우선주·리츠는 이번 수정 대상이 아니라 그대로 둠. 진입/청산/추격필터/휩쏘 등 나머지 로직은 변경 없음.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import io
import re
import time
import zipfile
import pandas as pd
import requests
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (SYSTEMS, WATCH_RATIO, MAX_CHASE_RATIO, check_turtle_breakout, notify_telegram,
                     is_market_alert_enabled, is_korea_trading_day)
from kis_client import kis_credentials_available, get_kis_daily_ohlc

MAX_WORKERS = 20
MARKETS = ['KOSPI', 'KOSDAQ']  # 2026-09-12: 'KOSPI' 단일값 -> 코스피+코스닥 리스트로 확대
MARKET_KEY = 'KR'
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'turtle_korea_result.csv')
ALERT_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'alert_settings.csv')
SCAN_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'scan_settings.csv')
TICKER_CACHE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'krx_tickers_cache.csv')

KRX_RETRY_COUNT = 5
KRX_RETRY_WAIT_SEC = 20
KIS_MASTER_URLS = {
    'KOSPI': "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    'KOSDAQ': "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}

KIS_AVAILABLE = kis_credentials_available()

# ===== 스캔 제외 대상: ETF / ETN / 스팩 (2026-10-13 17차 수정) =====
# 종목명이 "브랜드 + 공백"으로 시작할 때만 ETF로 본다. 그래서 '파워넷', 'HK이노엔',
# 'BNK금융지주'처럼 브랜드명으로 시작하는 일반 종목은 제외되지 않는다.
ETF_BRANDS = ('KODEX', 'TIGER', 'RISE', 'ACE', 'PLUS', 'SOL', 'KIWOOM', 'HANARO', '1Q', 'KoAct',
              'TIME', 'TIMEFOLIO', 'WON', 'BNK', 'HK', 'IBK', 'MIDAS', 'FOCUS', 'UNICORN', 'TREX',
              'TRUSTON', 'DAISHIN', 'ARIRANG', 'KOSEF', 'VITA', '파워', '마이티', '에셋플러스',
              '히어로즈', '타임폴리오')
ETF_BRAND_RE = re.compile(r'^(?:' + '|'.join(re.escape(b) for b in ETF_BRANDS) + r')\s')
ETF_SUFFIX_RE = re.compile(r'(?:액티브(?:\([^)]*\))?|X클래스)$')  # 브랜드 목록에 없는 ETF 대비
EXCLUDE_MAX_RATIO = 0.6  # 제외 비율이 이 값을 넘으면 규칙 오작동으로 보고 필터를 적용하지 않음


def exclusion_reason(name):
    """스캔에서 제외할 종목이면 사유('스팩'/'ETN'/'ETF(브랜드)'/'ETF(이름패턴)')를, 아니면 None."""
    s = str(name).strip()
    if '스팩' in s:
        return '스팩'
    if 'ETN' in s:
        return 'ETN'
    if ETF_BRAND_RE.match(s):
        return 'ETF(브랜드)'
    if ETF_SUFFIX_RE.search(s):
        return 'ETF(이름패턴)'
    return None


def is_excluded_security(name):
    return exclusion_reason(name) is not None


def filter_listing(listing):
    """종목 리스트(Code, Name)에서 ETF/ETN/스팩을 제거한다."""
    reasons = listing['Name'].apply(exclusion_reason)
    excluded = reasons.notna()
    total = len(listing)
    n_ex = int(excluded.sum())
    if total and n_ex / total > EXCLUDE_MAX_RATIO:
        print(f"[국장] ⚠️ 제외 대상이 {n_ex}/{total}개로 비정상적으로 많아 제외 필터를 적용하지 않습니다 "
              f"(종목명 형식이 바뀌었을 수 있음).")
        return listing
    counts = reasons[excluded].value_counts().to_dict()
    print(f"[국장] 스캔 제외 {n_ex}개 {counts} -> 스캔 대상 {total - n_ex}개")
    return listing[~excluded].reset_index(drop=True)


def is_manual_run():
    """GitHub Actions에서 사람이 'Run workflow' 버튼으로 수동 실행했는지 여부."""
    return os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'


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


def _get_one_market_listing_kis_master(market):
    """⓪ 한국투자증권 공식 종목마스터 파일 (시장 1개분). API 키 불필요, KRX/네이버와 무관한 서버."""
    url = KIS_MASTER_URLS.get(market)
    if not url:
        return None
    try:
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            mst_names = [n for n in zf.namelist() if n.lower().endswith('.mst')]
            if not mst_names:
                print(f"[kis마스터] {market} zip 안에 .mst 파일이 없습니다.")
                return None
            raw_bytes = zf.read(mst_names[0])
        raw_text = raw_bytes.decode('cp949', errors='ignore')
    except Exception as e:
        print(f"[kis마스터] {market} 다운로드/압축해제 실패: {e}")
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
        print(f"[kis마스터] {market} 파싱 결과가 비어있습니다 (파일 형식이 바뀌었을 수 있음).")
        return None
    return pd.DataFrame(rows, columns=['Code', 'Name']).drop_duplicates(subset=['Code'])


def get_kospi_listing_kis_master():
    """⓪ 한국투자증권 공식 종목마스터 파일 - 코스피+코스닥 두 시장을 각각 받아서 합침."""
    dfs = []
    for market in MARKETS:
        df = _get_one_market_listing_kis_master(market)
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True).drop_duplicates(subset=['Code'])


def _get_one_market_listing_fdr(market):
    for attempt in range(1, KRX_RETRY_COUNT + 1):
        try:
            listing = fdr.StockListing(market)
            if listing is not None and not listing.empty:
                return listing[['Code', 'Name']]
        except Exception as e:
            print(f"[fdr] {market} 목록 조회 실패 ({attempt}/{KRX_RETRY_COUNT}): {e}")
        if attempt < KRX_RETRY_COUNT:
            time.sleep(KRX_RETRY_WAIT_SEC)
    return None


def get_kospi_listing_fdr():
    """① FinanceDataReader (내부적으로 KRX 데이터를 씀) - 코스피+코스닥 합산."""
    dfs = []
    for market in MARKETS:
        df = _get_one_market_listing_fdr(market)
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True).drop_duplicates(subset=['Code'])


def _get_one_market_listing_pykrx(stock_mod, market):
    today = datetime.today().strftime('%Y%m%d')
    for attempt in range(1, KRX_RETRY_COUNT + 1):
        try:
            codes = stock_mod.get_market_ticker_list(today, market=market)
            if codes:
                rows = []
                for code in codes:
                    try:
                        name = stock_mod.get_market_ticker_name(code)
                    except Exception:
                        name = code
                    rows.append([code, name])
                if rows:
                    return pd.DataFrame(rows, columns=['Code', 'Name'])
        except Exception as e:
            print(f"[pykrx] {market} 목록 조회 실패 ({attempt}/{KRX_RETRY_COUNT}): {e}")
        if attempt < KRX_RETRY_COUNT:
            time.sleep(KRX_RETRY_WAIT_SEC)
    return None


def get_kospi_listing_pykrx():
    """② pykrx (이것도 결국 data.krx.co.kr을 씀 - fdr과 같은 이유로 같이 막힐 수 있음) - 코스피+코스닥 합산."""
    try:
        from pykrx import stock
    except ImportError:
        print("[pykrx] 패키지가 설치되어 있지 않아 폴백을 건너뜁니다 (requirements.txt에 pykrx 추가 필요).")
        return None

    dfs = []
    for market in MARKETS:
        df = _get_one_market_listing_pykrx(stock, market)
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True).drop_duplicates(subset=['Code'])


def _get_one_market_listing_naver(market):
    rows = []
    page = 1
    max_pages = 30
    try:
        while page <= max_pages:
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=100"
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
        print(f"[naver] {market} 목록 조회 실패: {e}")
        return None

    if not rows:
        return None
    return pd.DataFrame(rows, columns=['Code', 'Name']).drop_duplicates(subset=['Code'])


def get_kospi_listing_naver():
    """③ 네이버 금융 모바일 API - 코스피+코스닥 합산."""
    dfs = []
    for market in MARKETS:
        df = _get_one_market_listing_naver(market)
        if df is not None and not df.empty:
            dfs.append(df)
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True).drop_duplicates(subset=['Code'])


def get_kospi_listing_cache():
    """④ 최후의 수단: 직전에 성공했던 코스피+코스닥 통합 목록을 저장소에서 그대로 읽음."""
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
    """이름은 kospi지만 실제로는 MARKETS(코스피+코스닥) 통합 리스트를 반환한다."""
    for label, fn in (('kis마스터', get_kospi_listing_kis_master),
                       ('fdr', get_kospi_listing_fdr),
                       ('pykrx', get_kospi_listing_pykrx),
                       ('naver', get_kospi_listing_naver)):
        listing = fn()
        if listing is not None and not listing.empty:
            print(f"[국장] {label} 소스로 코스피+코스닥 종목 리스트 조회 성공 ({len(listing)}개)")
            save_kospi_listing_cache(listing)
            return listing
        print(f"[국장] {label} 소스 실패 -> 다음 소스 시도")

    print("[국장] kis마스터/fdr/pykrx/naver 모두 실패 -> 캐시된 마지막 성공 리스트로 폴백")
    return get_kospi_listing_cache()


def screen_korea():
    print(f"[국장] {'+'.join(MARKETS)} 종목 리스트 불러오는 중...")
    if KIS_AVAILABLE:
        print("[국장] KIS_APP_KEY/KIS_APP_SECRET 감지됨 - 가격 조회는 KIS 인증 API를 우선 사용합니다.")
    else:
        print("[국장] KIS 자격증명이 없어 가격 조회는 기존 fdr 방식으로 진행합니다.")

    listing = get_kospi_listing()
    if listing is None:
        return None

    # 종목 리스트 캐시(krx_tickers_cache.csv)는 필터 전 전체 목록 그대로 두고, 스캔 직전에만 제외한다.
    listing = filter_listing(listing)

    tickers = listing[['Code', 'Name']].values.tolist()
    print(f"총 {len(tickers)}개 종목 병렬 조회 시작 (ETF/ETN/스팩 제외, 가격필터 없이 코스피+코스닥 스캔)")

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


if __name__ == "__main__":
    # 2026-09-24 수정: 주말+공휴일(설/추석/임시공휴일 등)을 모두 포함해 실제 개장일인지
    # 판정. 수동 실행(workflow_dispatch)은 테스트/점검 목적이므로 휴장일에도 정상 진행.
    if not is_korea_trading_day() and not is_manual_run():
        print("[국장] 오늘은 국장 휴장일(주말 또는 공휴일)입니다 - 전체스캔과 알림을 건너뜁니다.")
        sys.exit(0)

    # 2026-10-09 추가: "국장전체스캔중지"/"국장전체스캔시작" 명령이 국장에도 실제로 먹히도록
    # (기존엔 미장/코인만 scan_settings.csv를 읽고 국장은 읽지 않았음).
    if not is_market_alert_enabled(MARKET_KEY, SCAN_SETTINGS_PATH):
        print("[국장] 전체스캔이 꺼져있는 상태입니다 - 이번 실행은 건너뜁니다.")
        sys.exit(0)

    alerts_enabled = is_market_alert_enabled(MARKET_KEY, ALERT_SETTINGS_PATH)
    if not alerts_enabled:
        print("[국장] 알림 중지 상태입니다 - 스캔/저장은 정상 진행하되 텔레그램 발송만 생략합니다.")

    def notify(msg):
        if alerts_enabled:
            notify_telegram(msg)


    df = screen_korea()

    if df is None:
        notify("[국장 전체스캔] 스캔 실패 - KOSPI/KOSDAQ 종목 리스트를 불러오지 못했습니다 "
               "(KRX 서버 오류로 추정, kis마스터/fdr/pykrx/네이버/캐시 모두 실패).")
        sys.exit(0)

    print(f"\n[국장] 신호 종목 {len(df)}개 발견")

    if os.path.exists(DATA_PATH):
        prev_df = pd.read_csv(DATA_PATH, dtype={'code': str})
        confirmed_prev = prev_df[prev_df['signal'] == '확정']
        if not confirmed_prev.empty and 'name' in confirmed_prev.columns:
            keep_mask = ~confirmed_prev['name'].apply(is_excluded_security)
            dropped = int((~keep_mask).sum())
            if dropped:
                print(f"기존 확정 종목 중 ETF/ETN/스팩 {dropped}개는 보존 대상에서 제외")
            confirmed_prev = confirmed_prev[keep_mask]
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
    print(f"[국장] 진입신호 {entry_cnt}개 / 관심신호 {watch_cnt}개 "
          f"(2026-10-09부터 전체스캔은 텔레그램 알림을 보내지 않음 - '확정 전환'만 recheck_korea.py가 알림)")

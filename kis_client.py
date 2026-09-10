# -*- coding: utf-8 -*-
"""한국투자증권(KIS) Open API 공용 클라이언트.

- OAuth 접근토큰 발급 (프로세스 내 메모리 캐시 - 파일로 저장하지 않음.
  스캔은 한 번의 프로세스 실행 안에서 끝나므로 굳이 파일 캐시가 필요 없고,
  토큰을 커밋된 파일에 남기면 git 히스토리에 접속토큰이 노출되는 보안 문제가
  생기기 때문에 의도적으로 파일 캐시를 두지 않음)
- 국내주식기간별시세(일봉) 조회, 100일 제한을 넘지 않도록 자동으로 여러 번
  나눠서 호출 후 하나로 합침
- KIS_APP_KEY / KIS_APP_SECRET 환경변수가 없으면 모든 함수가 조용히 None을
  반환함 -> 호출하는 쪽에서 다른 소스로 자연스럽게 폴백 가능

사용법 (환경변수):
  KIS_APP_KEY, KIS_APP_SECRET  (GitHub Secrets에 등록 후 workflow env로 주입)
"""

import os
import time
import threading
from datetime import datetime, timedelta

import requests
import pandas as pd

KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

# ===== 접근토큰 (프로세스 내 메모리 캐시) =====
_token_lock = threading.Lock()
_cached_token = {'value': None, 'expires_at': 0.0}

# ===== 초당 호출 제한 (KIS 실전계좌 REST 기준 여유있게 초당 10회로 제한) =====
_rate_lock = threading.Lock()
_last_call_time = [0.0]
MIN_CALL_INTERVAL = 0.1  # 초당 약 10회


def _rate_limit():
    with _rate_lock:
        now = time.time()
        wait = MIN_CALL_INTERVAL - (now - _last_call_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_call_time[0] = time.time()


def kis_credentials_available():
    return bool(os.environ.get('PSWqb0iXAiPNZtVwRV40s8ALkNmC1H4JchfR')) and bool(os.environ.get('ZA/1e2YVtVk+c3rVB3EVEPetCz+G3tO14P/I0kK6LSw0HQbH1LX6q+F+7fWSZvAfT05l74qesZZsFmxObIfSXkKylz5rwgY+ZYxeQRKIV5vHDtRZY5e0SKu7jT5lLwEanZD56LGQOE294E/fPi+GqsnLjItnE0FLD37Z2H6R10gy8KSTqDA='))


def get_kis_access_token():
    """OAuth 접근토큰 발급. 이미 유효한 토큰이 메모리에 있으면 재사용."""
    appkey = os.environ.get('KIS_APP_KEY')
    appsecret = os.environ.get('KIS_APP_SECRET')
    if not appkey or not appsecret:
        return None

    with _token_lock:
        if _cached_token['value'] and _cached_token['expires_at'] > time.time() + 60:
            return _cached_token['value']

        try:
            res = requests.post(
                f"{KIS_BASE_URL}/oauth2/tokenP",
                json={"grant_type": "client_credentials", "appkey": appkey, "appsecret": appsecret},
                timeout=10,
            )
            res.raise_for_status()
            data = res.json()
            token = data.get('access_token')
            expires_in = int(data.get('expires_in', 86400))
            if token:
                _cached_token['value'] = token
                _cached_token['expires_at'] = time.time() + expires_in
                return token
            print(f"[kis] 토큰 응답에 access_token이 없습니다: {data}")
        except Exception as e:
            print(f"[kis] 접근토큰 발급 실패: {e}")
    return None


def _fetch_chunk(code, start_date, end_date, token, appkey, appsecret, retry=1):
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": appkey,
        "appsecret": appsecret,
        "tr_id": "FHKST03010100",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",  # 0: 수정주가(분할/배당 반영), 1: 원주가
    }

    for attempt in range(retry + 1):
        _rate_limit()
        try:
            res = requests.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers=headers, params=params, timeout=10,
            )
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(1.0 * (attempt + 1))
                continue
            res.raise_for_status()
            body = res.json()
            if body.get('rt_cd') != '0':
                # EGW00201류 초당 거래건수 초과 등은 짧게 쉬고 1번 재시도
                if attempt < retry:
                    time.sleep(1.0)
                    continue
                print(f"[kis] {code} 시세 조회 오류: {body.get('msg1')}")
                return []
            return body.get('output2', []) or []
        except Exception as e:
            if attempt < retry:
                time.sleep(0.5)
                continue
            print(f"[kis] {code} 시세 조회 실패: {e}")
            return []
    return []


def get_kis_daily_ohlc(code, days=300):
    """KIS 국내주식기간별시세로 최근 `days`일치 일봉을 조회해서 High/Low/Close 등을
    담은 DataFrame으로 반환한다. 자격증명이 없거나 실패하면 None을 반환하므로
    호출하는 쪽에서 다른 소스(fdr 등)로 자연스럽게 폴백하면 된다."""
    appkey = os.environ.get('KIS_APP_KEY')
    appsecret = os.environ.get('KIS_APP_SECRET')
    if not appkey or not appsecret:
        return None

    token = get_kis_access_token()
    if not token:
        return None

    end_cursor = datetime.today()
    remaining = days
    all_rows = []

    while remaining > 0:
        chunk_days = min(remaining, 95)  # 한 번에 최대 100건 제한에 여유를 둔 값(달력일 기준)
        start_cursor = end_cursor - timedelta(days=chunk_days)
        rows = _fetch_chunk(code, start_cursor, end_cursor, token, appkey, appsecret)
        if rows:
            all_rows.extend(rows)
        end_cursor = start_cursor - timedelta(days=1)
        remaining -= chunk_days

    if not all_rows:
        return None

    try:
        df = pd.DataFrame(all_rows)
        df = df.rename(columns={
            'stck_bsop_date': 'Date', 'stck_oprc': 'Open', 'stck_hgpr': 'High',
            'stck_lwpr': 'Low', 'stck_clpr': 'Close', 'acml_vol': 'Volume',
        })
        needed = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
        if not all(c in df.columns for c in needed):
            print(f"[kis] {code} 응답 필드 형식이 예상과 다릅니다: {list(df.columns)}")
            return None
        df['Date'] = pd.to_datetime(df['Date'])
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['Close']).drop_duplicates(subset=['Date'])
        df = df.set_index('Date').sort_index()
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]
    except Exception as e:
        print(f"[kis] {code} 데이터 파싱 실패: {e}")
        return None

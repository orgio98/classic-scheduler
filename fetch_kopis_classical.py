"""
클래식 공연 통합 사이트 - KOPIS 데이터 파이프라인

세종문화회관 / 예술의전당 / 롯데콘서트홀 / 고양아람누리 네 곳의 클래식(서양음악)·무용 공연을
KOPIS API로 가져와서 장르별로 분류한 뒤 data/performances.json 으로 저장한다.

필요 환경변수:
    KOPIS_API_KEY - KOPIS 오픈API 서비스키

사용법:
    KOPIS_API_KEY=xxxx python fetch_kopis_classical.py

수집 항목:
    공연명 / 기간 / 공연장 / 포스터 / 출연진(지휘·협연) / 제작진 /
    좌석별 가격 / 예매처 링크(relates) / 공연시간 안내

[중요] 공연장 필터링
  KOPIS 시설검색은 파라미터가 무시되면 전국 시설을 그대로 반환한다.
  (부산 영화의전당 공연이 "세종문화회관"으로 잡히던 버그의 원인)
  그래서 응답을 그대로 믿지 않고 시설명으로 직접 검증 + 상세에서 교차 검증한다.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from fetch_ticket_open import collect_ticket_opens, match_ticket_opens

API_BASES = ["https://www.kopis.or.kr/openApi/restful", "http://www.kopis.or.kr/openApi/restful"]
API_BASE = API_BASES[0]   # 연결이 안 되면 다른 주소로 바꿔 시도하고, 되는 주소를 계속 쓴다
SERVICE_KEY = os.environ.get("KOPIS_API_KEY", "").strip()

# ── 공연장 정의 ────────────────────────────────────────────────────────────
# 시설명(fcltynm)으로 어느 기관인지 판정한다.
#
# [중요] 지역명 블랙리스트 방식은 쓰지 않는다.
#   전국에 "경주예술의전당, 계룡문화예술의전당, 군산예술의전당, 서귀포예술의전당,
#   안동문화예술의전당, 영광예술의전당, 완도문화예술의전당, 제천예술의전당,
#   진천예술의전당, 화성예술의전당..." 처럼 같은 이름이 끝없이 많아서
#   제외 목록을 아무리 늘려도 계속 새는 구조였다.
#
#   대신 "이름이 그 기관명으로 시작하는가"로 판정한다.
#   앞에 지역명이 붙어 있으면(예: 경주예술의전당) 다른 기관이므로 자동 제외된다.
#     "예술의전당 [서울] (콘서트홀)"  -> 시작함  -> 서울 예술의전당 O
#     "경주예술의전당"                -> 시작 안 함 -> 제외
#
# prefix 는 순서가 중요하다. 더 구체적인 이름이 먼저 와야 한다.
VENUE_RULES = [
    {
        "label": "세종문화회관",
        "prefix": ["세종문화회관"],
        # 홀 이름만 들어오는 경우도 대비
        "contains": ["세종대극장", "세종체임버홀", "세종S씨어터", "세종M씨어터"],
    },
    {
        "label": "예술의전당",              # 서울 서초구 소재
        "prefix": ["예술의전당"],
    },
    {
        "label": "롯데콘서트홀",            # 서울 잠실 롯데월드타워 소재
        "prefix": ["롯데콘서트홀"],
        "contains": ["롯데콘서트홀"],
    },
    {
        "label": "고양아람누리",
        "prefix": ["고양아람누리", "아람누리"],
        "contains": ["아람음악당", "아람극장"],
    },
]

# KOPIS 시설 검색에 사용할 키워드
SEARCH_TERMS = ["세종문화회관", "예술의전당", "롯데콘서트홀", "고양아람누리"]

LOOKAHEAD_DAYS = 180

GENRE_ORCHESTRA = "오케스트라"
GENRE_OPERA = "오페라"
GENRE_DANCE = "발레·무용"
GENRE_CHAMBER = "실내악·독주"
GENRE_CHOIR = "합창"

OUTPUT_PATH = Path("data/performances.json")

# 목록 조회 실패를 모아 두었다가, 결과가 비면 원인과 함께 '실패'로 끝낸다.
LIST_ERRORS: list[str] = []


# KOPIS 방화벽은 2026년 11월 말부터 파이썬 기본 User-Agent 요청을 "400 Request Blocked"로 막는다.
# 앱 이름을 밝히고, 요청 간격을 두고, 막히면 잠시 기다렸다 다시 시도한다.
USER_AGENT = "classic-scheduler/1.0 (+https://github.com/orgio98/classic-scheduler)"
MIN_INTERVAL = 1.0        # 초. KOPIS 호출 사이 최소 간격 (막히면 자동으로 늘어난다)
MAX_INTERVAL = 6.0
BLOCK_RETRIES = 4         # Request Blocked를 받으면 다시 시도할 횟수
BLOCK_WAIT = 20           # 초. 다시 시도하기 전 기다리는 시간 (시도할수록 길어진다)
DETAIL_REFRESH_DAYS = 7   # 이미 받은 공연의 상세정보는 이 주기로만 다시 받는다
_last_call = 0.0


REQUEST_TIMEOUT = 15      # 초
NET_FAIL_LIMIT = 6        # 두 주소 모두 연결이 안 되는 일이 연달아 이만큼이면 수집을 멈춘다
_net_fails = 0


class KopisBlocked(RuntimeError):
    pass


class KopisUnreachable(RuntimeError):
    """KOPIS에 계속 연결되지 않음. 시간만 쓰지 말고 수집을 멈춘다 (기존 데이터는 유지)."""


def _fetch(url: str) -> bytes:
    global _last_call
    wait = MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            body = res.read()
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if "Request Blocked" in detail:
            raise KopisBlocked("KOPIS 방화벽이 요청을 막았습니다 (400 Request Blocked)") from None
        raise
    if b"Request Blocked" in body[:2000]:
        raise KopisBlocked("KOPIS 방화벽이 요청을 막았습니다 (Request Blocked)")
    return body


def _get(path: str, **params) -> ET.Element:
    global MIN_INTERVAL
    for attempt in range(BLOCK_RETRIES + 1):
        try:
            return _get_once(path, **params)
        except KopisBlocked as e:
            MIN_INTERVAL = min(MAX_INTERVAL, MIN_INTERVAL * 1.5)   # 막히면 속도를 늦춘다
            if attempt == BLOCK_RETRIES:
                raise RuntimeError(str(e)) from None
            wait = BLOCK_WAIT * (attempt + 1)
            print(f"  KOPIS 차단 응답 → {wait}초 뒤 다시 시도 ({attempt + 1}/{BLOCK_RETRIES}), 간격 {MIN_INTERVAL:.1f}초", file=sys.stderr, flush=True)
            time.sleep(wait)


def _get_once(path: str, **params) -> ET.Element:
    if not SERVICE_KEY:
        print("오류: KOPIS_API_KEY 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)
    global API_BASE, MIN_INTERVAL, _net_fails
    params = {"service": SERVICE_KEY, **params}
    query = urlencode(params)
    try:
        while True:
            body, net_err = None, None
            for base in [API_BASE] + [b for b in API_BASES if b != API_BASE]:
                try:
                    body = _fetch(f"{base}/{path}?{query}")
                except KopisBlocked:
                    raise
                except HTTPError:
                    raise
                except (URLError, TimeoutError, OSError) as e:
                    net_err = getattr(e, "reason", e)
                    continue
                if base != API_BASE:
                    print(f"  ! {API_BASE} 연결 안 됨 → {base} 로 바꿔 씁니다", file=sys.stderr, flush=True)
                    API_BASE = base
                break
            if body is not None:
                _net_fails = 0
                break
            _net_fails += 1
            if _net_fails >= NET_FAIL_LIMIT:
                raise KopisUnreachable(f"KOPIS에 {_net_fails}번 연달아 연결하지 못했습니다 (https·http 모두): {net_err}")
            raise RuntimeError(f"네트워크 오류: {net_err}")
    except KopisBlocked as e:
        # 차단은 기다렸다 다시 시도한다 (재귀 대신 아래에서 반복)
        raise
    except HTTPError as e:
        # 지금까지는 "HTTP Error 400: Bad Request" 라는 뭉뚱그려진 메시지만 보였다.
        # KOPIS는 400/401 등에서도 실제 원인(서비스키 문제 / 트래픽(호출량) 초과 /
        # 파라미터 오류 등)을 응답 본문에 담아 보내는 경우가 많으므로, 그 본문을
        # 함께 노출해야 정확한 진단이 가능하다.
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace").strip()[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason}" + (f" | 응답: {detail}" if detail else "")) from None
    except URLError as e:
        raise RuntimeError(f"네트워크 오류: {e.reason}") from None
    return ET.fromstring(body)


def _clean_name(fcltynm: str) -> str:
    """시설명 앞의 법인 표기 등을 떼어내 접두 비교가 가능하게 만든다."""
    s = (fcltynm or "").strip()
    s = re.sub(r"^\(\s*(재|사|주|재단법인|사단법인)\s*\)\s*", "", s)
    return s.strip()


def resolve_venue(fcltynm: str) -> str | None:
    """
    시설명으로 기관을 판정한다. 해당 없으면 None(= 대상 아님, 제외).
    접두 일치를 쓰므로 "경주예술의전당" 같은 타 지역 시설은 자동으로 걸러진다.
    """
    name = _clean_name(fcltynm)
    if not name:
        return None
    for rule in VENUE_RULES:
        if any(name.startswith(p) for p in rule.get("prefix", [])):
            return rule["label"]
        if any(k in name for k in rule.get("contains", [])):
            return rule["label"]
    return None


# KOPIS 공연장 코드는 "공연시설 ID(mt10id)"와 별개인 "공연장 코드(prfplccd)"다.
# 공연목록 API의 prfplccd에는 반드시 FCxxxxxx-xx 형식의 공연장 코드를 넣어야 한다.
# 2026 KOPIS 공통코드에 등록된 대상 공연장 코드만 사용한다.
TARGET_HALLS = {
    "세종문화회관": [
        ("FC000020-01", "세종문화회관", "대극장"),
        ("FC000020-02", "세종문화회관", "세종체임버홀"),
        ("FC000020-03", "세종문화회관", "M 씨어터"),
        ("FC000020-07", "세종문화회관", "S 씨어터"),
    ],
    "예술의전당": [
        ("FC000001-01", "예술의전당", "CJ 토월극장"),
        ("FC000001-02", "예술의전당", "리사이틀홀"),
        ("FC000001-03", "예술의전당", "콘서트홀"),
        ("FC000001-04", "예술의전당", "자유소극장"),
        ("FC000001-05", "예술의전당", "IBK 챔버홀"),
        ("FC000001-06", "예술의전당", "오페라극장"),
        ("FC000001-11", "예술의전당", "신세계스퀘어 야외무대"),
    ],
    "롯데콘서트홀": [
        ("FC001513-01", "롯데콘서트홀", "롯데콘서트홀"),
    ],
    "고양아람누리": [
        ("FC000127-01", "고양아람누리", "아람음악당"),
        ("FC000127-02", "고양아람누리", "새라새극장"),
        ("FC000127-03", "고양아람누리", "아람극장"),
        ("FC000127-04", "고양아람누리", "노루목야외극장"),
    ],
}


def find_facilities() -> list[dict]:
    """API의 시설명 검색 결과에 의존하지 않고 공식 공연장 코드로 대상을 확정한다."""
    facilities = []
    for venue, halls in TARGET_HALLS.items():
        for prfplccd, fcltynm, hallnm in halls:
            facilities.append({
                "mt10id": prfplccd.split("-")[0],
                "prfplccd": prfplccd,
                "fcltynm": fcltynm,
                "prfplcnm": hallnm,
                "venue": venue,
            })
    return facilities

def _date_windows(stdate: str, eddate: str):
    """KOPIS pblprfr의 최대 31일 조회기간에 맞춰 날짜 구간을 만든다."""
    start = date(int(stdate[:4]), int(stdate[4:6]), int(stdate[6:8]))
    end = date(int(eddate[:4]), int(eddate[4:6]), int(eddate[6:8]))
    cur = start
    while cur <= end:
        window_end = min(cur + timedelta(days=30), end)
        yield cur.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")
        cur = window_end + timedelta(days=1)


def find_performance_ids(prfplccd: str, stdate: str, eddate: str) -> list[dict]:
    """
    공연장 코드 기준으로 공연목록을 장르 제한 없이 수집한다.

    KOPIS 목록 단계에서 CCCA/BBBC/EEEA를 따로 조회하면 장르 등록 방식에
    따라 공연이 빠질 수 있다. 따라서 여기서는 shcate를 넣지 않고 전체 목록을
    받은 뒤, 목록의 genrenm + 공연명으로 클래식/무용 후보를 골라 상세를 조회한다.

    stdate~eddate는 KOPIS의 최대 31일 제한 때문에 30일 단위로 나눈다.
    """
    rows = []
    seen = set()

    for ws, we in _date_windows(stdate, eddate):
        page = 1
        window_count = 0

        while True:
            try:
                root = _get(
                    "pblprfr",
                    stdate=ws,
                    eddate=we,
                    cpage=str(page),
                    rows="100",
                    prfplccd=prfplccd,
                )
            except KopisUnreachable:
                raise
            except Exception as e:
                LIST_ERRORS.append(f"{prfplccd}/{ws}~{we}/p{page}: {e}")
                print(
                    f"  경고: 공연목록 조회 실패 ({prfplccd}/{ws}~{we}/p{page}): {e}",
                    file=sys.stderr,
                )
                break

            dbs = root.findall("db")
            if not dbs:
                break

            for db in dbs:
                mt20id = (db.findtext("mt20id") or "").strip()
                if not mt20id or mt20id in seen:
                    continue

                genrenm = (db.findtext("genrenm") or "").strip()
                prfnm = (db.findtext("prfnm") or "").strip()

                # 목록 단계에서 명백한 비대상 장르를 제거한다.
                # CCCA=서양음악, BBBC=무용은 모두 후보.
                # EEEA=복합은 제목에 클래식/무용 단서가 있을 때만 후보.
                raw = f"{genrenm} {prfnm}".lower()
                candidate = (
                    "서양음악" in genrenm
                    or "클래식" in genrenm
                    or "무용" in genrenm
                    or any(k in raw for k in (
                        "오페라", "합창", "교향", "오케스트라", "필하모닉",
                        "심포니", "관현악", "발레", "무용", "댄스",
                        "피아노", "바이올린", "첼로", "비올라", "실내악",
                        "리사이틀", "독주", "앙상블", "협주곡", "콘체르토",
                    ))
                )

                if candidate:
                    seen.add(mt20id)
                    rows.append({
                        "mt20id": mt20id,
                        "genrenm": genrenm,
                        "prfnm": prfnm,
                    })
                    window_count += 1

            if len(dbs) < 100:
                break
            page += 1
            pass  # 간격은 _fetch()의 MIN_INTERVAL이 맡는다

        print(f"    {prfplccd} {ws}~{we}: 후보 {window_count}건", flush=True)
        pass  # 간격은 _fetch()의 MIN_INTERVAL이 맡는다

    return rows

def norm_date(s: str) -> str:
    """
    KOPIS는 날짜를 '2026.08.30' 형식으로 주기도 하고 '20260830' 으로 주기도 한다.
    문자열 비교(달력/목록 필터)가 올바르게 동작하려면 YYYYMMDD로 통일해야 한다.
    """
    digits = re.sub(r"[^0-9]", "", s or "")
    return digits if len(digits) == 8 else (s or "").strip()


def parse_prices(pcseguidance: str) -> list[dict]:
    """
    '전석 30,000원' / 'R석 50,000원, S석 30,000원' 같은 자유텍스트를
    [{"seat": "R석", "amount": 50000, "text": "R석 50,000원"}, ...] 로 파싱한다.
    형식이 제각각이라 실패할 수 있으므로 원문(price_text)도 함께 보존한다.
    """
    if not pcseguidance:
        return []
    items = []
    # "OO석 12,000원" 패턴을 모두 찾는다
    for m in re.finditer(r"([가-힣A-Za-z\s·()]{0,10}?석|전석|일반|학생|청소년|어린이)?\s*([\d,]+)\s*원", pcseguidance):
        seat = (m.group(1) or "").strip()
        seat = re.sub(r"^(년|월|일|회|차)\s*", "", seat).strip()
        raw = m.group(2).replace(",", "")
        if not raw.isdigit():
            continue
        amount = int(raw)
        if amount < 1000:  # 오탐 방지 (예: '2026년' 같은 숫자)
            continue
        items.append({
            "seat": seat or "가격",
            "amount": amount,
            "text": m.group(0).strip(),
        })
    return items


def parse_booking_links(db: ET.Element) -> list[dict]:
    """KOPIS 상세의 <relates><relate> 에서 예매처 이름/링크를 뽑는다."""
    links = []
    relates = db.find("relates")
    if relates is None:
        return links
    for rel in relates.findall("relate"):
        name = (rel.findtext("relatenm") or "").strip()
        url = (rel.findtext("relateurl") or "").strip()
        if url and url.startswith("http"):
            links.append({"name": name or "예매처", "url": url})
    return links


# 장기간(예: 몇 달) 걸친 공연 중에는 매일 여는 게 아니라 시즌마다 한 번씩만
# 여는 '연간 시리즈'가 있다. 예: 롯데콘서트홀 '오르간 오딧세이'는 2.25 / 7.22 / 12.16
# 세 번뿐인데, KOPIS API에는 시작일(2.25)~종료일(12.16)만 있고 그 사이 모든 날에
# 공연이 있는 것처럼 보인다. (KOPIS 자체의 한계 — 회차별 날짜 목록을 제공하지 않는다)
#
# '공연시간 안내'(dtguidance) 텍스트에 실제 날짜가 적혀있는 경우가 있어 이를 파싱해
# 시도해본다. 실패하면 최소한 '매일 공연'으로는 보이지 않도록 시작일/종료일만 남긴다.
LONG_RUN_THRESHOLD_DAYS = 30  # 이보다 길면 '연속 공연'이 아닐 수 있다고 의심

_DATE_PATTERNS = [
    re.compile(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})"),      # 2026.02.25 / 2026-02-25
    re.compile(r"(?<!\d)(\d{1,2})[.\-](\d{1,2})(?!\d)"),      # 02.25 (연도 없음, start_date 연도 사용)
]


def extract_specific_dates(dtguidance: str, start_date: str, end_date: str) -> list[str]:
    """
    '공연시간 안내' 텍스트에서 구체적인 날짜들을 뽑아본다.
    start_date~end_date 범위 밖의 숫자(예: 상영시간 90분의 '90')는 자연히 걸러진다.
    2개 미만이면(=구체적 날짜 나열이 아니라고 판단) 빈 리스트를 반환한다.
    """
    if not dtguidance or not start_date or not end_date:
        return []

    year = start_date[:4]
    found = set()

    for m in re.finditer(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", dtguidance):
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        found.add(f"{y}{mo}{d}")

    # 연도 없이 "월.일"만 있는 경우 (예: "2.25, 7.22, 12.16")도 시도.
    # 단, 위에서 이미 연도 포함 날짜를 찾았다면 혼선을 피하기 위해 생략한다.
    if not found:
        for m in re.finditer(r"(?<!\d)(\d{1,2})[.\-](\d{1,2})(?!\d)", dtguidance):
            mo, d = m.group(1).zfill(2), m.group(2).zfill(2)
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                found.add(f"{year}{mo}{d}")

    valid = sorted(d for d in found if start_date <= d <= end_date)
    return valid if len(valid) >= 2 else []


def fetch_detail(mt20id: str) -> dict:
    try:
        root = _get(f"pblprfr/{mt20id}")
    except KopisUnreachable:
        raise
    except Exception as e:
        print(f"  경고: 상세 조회 실패 ({mt20id}): {e}", file=sys.stderr)
        return {}
    db = root.find("db")
    if db is None:
        return {}

    def text(tag):
        return (db.findtext(tag) or "").strip()

    price_text = text("pcseguidance")
    prices = parse_prices(price_text)

    result = {
        "mt20id": mt20id,
        "name": text("prfnm"),
        "start_date": norm_date(text("prfpdfrom")),
        "end_date": norm_date(text("prfpdto")),
        "facility_name": text("fcltynm"),
        "poster": text("poster"),
        "cast": text("prfcast"),
        "crew": text("prfcrew"),
        "runtime": text("prfruntime"),
        "age": text("prfage"),
        "company": text("entrpsnmH"),
        "price_text": price_text,                       # 원문 그대로
        "prices": prices,                               # 좌석별 파싱 결과
        "price_min": min([p["amount"] for p in prices]) if prices else None,
        "price_max": max([p["amount"] for p in prices]) if prices else None,
        "booking_links": parse_booking_links(db),       # 예매처 링크
        "schedule": text("dtguidance"),                 # 요일별 공연시간
        "genre_raw": text("genrenm"),
        "state": text("prfstate"),
    }

    # 장기 시리즈 감지: 기간이 길고, 공연시간 안내에서 구체적 날짜를 뽑을 수 있으면
    # 그 날짜만 실제 공연일로 표시한다. 못 뽑으면 최소한 '매일 공연'으로는 안 보이게
    # 시작일/종료일만 남긴다 (완전히 정확하진 않지만 훨씬 낫다).
    start_date, end_date = result["start_date"], result["end_date"]
    if start_date and end_date and len(start_date) == 8 and len(end_date) == 8:
        try:
            span_days = (
                date(int(end_date[:4]), int(end_date[4:6]), int(end_date[6:8]))
                - date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
            ).days
        except ValueError:
            span_days = 0

        if span_days >= LONG_RUN_THRESHOLD_DAYS:
            specific = extract_specific_dates(result["schedule"], start_date, end_date)
            if specific:
                result["specific_dates"] = specific
                result["irregular_long_run"] = False
                print(f"    날짜 파싱 성공: {result['name']} -> {specific}")
            else:
                # 파싱 실패: 시작일/종료일 두 곳에만 표시하도록 프론트에 신호를 준다.
                result["specific_dates"] = [start_date, end_date]
                result["irregular_long_run"] = True
                print(f"    경고: 장기 공연인데 날짜 파싱 실패 (시작/종료일만 표시): "
                      f"{result['name']} ({start_date}~{end_date}) "
                      f"dtguidance={result['schedule'][:60]!r}")

    return result


def guess_genre(perf: dict) -> tuple[str | None, bool]:
    """상세 장르명과 공연명으로 화면 장르를 결정한다."""
    raw = f"{perf.get('genre_raw', '')} {perf.get('name', '')}".lower()

    if any(k in raw for k in ("발레", "무용", "댄스", "dance", "ballet")):
        return GENRE_DANCE, False
    if "오페라" in raw or "opera" in raw:
        return GENRE_OPERA, False
    if any(k in raw for k in ("합창", "칸타타", "choral", "choir")):
        return GENRE_CHOIR, False
    if any(k in raw for k in (
        "오케스트라", "필하모닉", "교향악단", "심포니", "관현악",
        "orchestra", "symphony", "philharmonic"
    )):
        return GENRE_ORCHESTRA, False
    if any(k in raw for k in (
        "피아노", "바이올린", "첼로", "비올라", "플루트", "클라리넷",
        "오보에", "바순", "호른", "트럼펫", "하프",
        "실내악", "독주", "리사이틀", "앙상블", "듀오", "트리오",
        "콰르텟", "퀄텟", "quartet", "recital", "chamber"
    )):
        return GENRE_CHAMBER, False

    if any(k in raw for k in ("서양음악", "클래식", "classic")):
        return GENRE_CHAMBER, True

    # EEEA(복합)에서 명확한 클래식/무용 단서가 없는 경우 제외.
    return None, False

def main():
    today = date.today()
    stdate = today.strftime("%Y%m%d")
    eddate = (today + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y%m%d")

    print(f"[{today}] 공연장 코드 조회 중...")
    facilities = find_facilities()
    by_label = {}
    for f in facilities:
        by_label.setdefault(f["venue"], []).append(f)
    for label, fs in by_label.items():
        print(f"  {label}: {len(fs)}개 공연장")
        for f in fs:
            print(f"     - {f['fcltynm']} ({f['mt10id']})")

    if not facilities:
        print("오류: 공연장을 하나도 찾지 못했습니다. API 키/응답을 확인하세요.", file=sys.stderr)
        sys.exit(1)

    # 지난 실행 결과: 상세정보를 다시 받지 않고 재사용하거나, 상세 조회가 막혔을 때 대신 쓴다.
    prev_by_id = {}
    prev_generated = today
    if OUTPUT_PATH.exists():
        try:
            _prev = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            prev_by_id = {p.get("mt20id"): p for p in _prev.get("performances", []) if p.get("mt20id")}
            prev_generated = date.fromisoformat(_prev.get("generated_at") or today.isoformat())
        except Exception:
            prev_by_id = {}
    DERIVED = ("venue", "genre", "genre_guessed", "is_new", "ticket_open")

    def reusable(prev: dict) -> bool:
        """이미 받은 상세정보를 이번엔 재사용해도 되는지. 공연마다 요일을 나눠 일주일에 한 번씩 새로 받는다."""
        try:
            fetched = date.fromisoformat(prev.get("detail_fetched") or prev_generated.isoformat())
        except ValueError:
            return False
        age = (today - fetched).days
        if age >= DETAIL_REFRESH_DAYS + 3:
            return False
        digits = "".join(ch for ch in prev.get("mt20id", "") if ch.isdigit()) or "0"
        return not (age >= 1 and int(digits) % DETAIL_REFRESH_DAYS == today.toordinal() % DETAIL_REFRESH_DAYS)

    stats = {"reused": 0, "fetched": 0, "fallback": 0}

    print(f"\n공연 목록 조회 중 ({stdate} ~ {eddate})...")
    results = {}
    hall_counts = {}

    for f in facilities:
        hall_key = f["prfplccd"]
        hall_counts.setdefault(f["venue"], 0)

        candidates = find_performance_ids(hall_key, stdate, eddate)
        print(f"  {hall_key}: 클래식/무용 후보 {len(candidates)}건", flush=True)

        for row in candidates:
            mt20id = row["mt20id"]
            if mt20id in results:
                continue

            prev = prev_by_id.get(mt20id)
            if prev and reusable(prev):
                detail = {k: v for k, v in prev.items() if k not in DERIVED}
                detail.setdefault("detail_fetched", prev_generated.isoformat())
                stats["reused"] += 1
            else:
                detail = fetch_detail(mt20id)
                if detail:
                    detail["detail_fetched"] = today.isoformat()
                    stats["fetched"] += 1
                elif prev:
                    # 상세 조회가 막혀도 공연을 빼지 않고 지난번 정보를 그대로 쓴다.
                    detail = {k: v for k, v in prev.items() if k not in DERIVED}
                    stats["fallback"] += 1
                else:
                    print(f"  상세정보 없음: {mt20id}", file=sys.stderr)
                    continue

            label = resolve_venue(detail.get("facility_name", ""))
            if label is None:
                continue

            genre, guessed = guess_genre(detail)
            if genre is None:
                continue

            detail["venue"] = label
            detail["genre"] = genre
            detail["genre_guessed"] = guessed
            results[mt20id] = detail
            pass  # 간격은 _fetch()의 MIN_INTERVAL이 맡는다

        hall_counts[f["venue"]] += 1

    performances = sorted(results.values(), key=lambda p: p.get("start_date", ""))
    print(f"  상세정보: 새로 받음 {stats['fetched']}건 · 재사용 {stats['reused']}건 · 막혀서 지난 정보 사용 {stats['fallback']}건")

    # API 장애/차단으로 0건 또는 비정상적으로 적은 결과가 나온 경우
    # 기존 정상 데이터를 통째로 덮어쓰지 않는다.
    previous = None
    previous_perfs = []
    if OUTPUT_PATH.exists():
        try:
            previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            previous_perfs = previous.get("performances", []) or []
        except Exception:
            previous = None

    def keep_previous_and_fail(reason: str):
        # 기존 데이터는 그대로 두되, 실행은 '실패'로 끝내서 GitHub이 알림을 보내게 한다.
        # (예전에는 성공으로 끝나서 며칠째 갱신이 멈춘 걸 알아채기 어려웠다.)
        print(f"오류 방지: {reason} 기존 {len(previous_perfs)}건을 유지합니다.", file=sys.stderr)
        if LIST_ERRORS:
            print(f"  공연목록 조회 실패 {len(LIST_ERRORS)}회. 첫 오류: {LIST_ERRORS[0]}", file=sys.stderr)
        sys.exit(1)

    if not performances and previous_perfs:
        keep_previous_and_fail("이번 실행 결과가 0건이라")

    if previous_perfs and len(performances) < max(10, int(len(previous_perfs) * 0.60)):
        keep_previous_and_fail(f"이번 결과 {len(performances)}건이 이전 {len(previous_perfs)}건의 60% 미만이라")
    if LIST_ERRORS:
        print(f"  참고: 공연목록 조회 실패 {len(LIST_ERRORS)}회 (첫 오류: {LIST_ERRORS[0]})", file=sys.stderr)
    print(f"\n총 {len(performances)}건 수집")

    by_venue = {}
    no_link = 0
    for p in performances:
        by_venue[p["venue"]] = by_venue.get(p["venue"], 0) + 1
        if not p.get("booking_links"):
            no_link += 1
    for v, c in by_venue.items():
        print(f"  {v}: {c}건")
    print(f"  예매링크 없는 공연: {no_link}건")

    # ── 티켓오픈 공지 수집 후 공연에 매칭 ──────────────────────────────
    print("\n티켓오픈 공지 수집 중...")
    opens = collect_ticket_opens()
    matched = match_ticket_opens(performances, opens)
    print(f"  공연과 매칭된 티켓오픈 공지: {matched}건")

    # ── 신규 공연 감지 ────────────────────────────────────────────────
    # 지난 실행 결과와 비교해서 이번에 새로 등장한 공연을 표시한다.
    # KOPIS에 공연이 올라오는 시점은 대체로 예매 오픈과 가까워서,
    # 티켓오픈 공지가 없는 공연장(예술의전당 등)에도 NEW 표시가 붙는다.
    prev_ids = {p.get("mt20id") for p in previous_perfs}

    new_count = 0
    for p in performances:
        # 이전 실행에 없던 공연 = 신규. 단 첫 실행(prev_ids 비어있음)에는 표시하지 않는다.
        p["is_new"] = bool(prev_ids) and p.get("mt20id") not in prev_ids
        if p["is_new"]:
            new_count += 1
    print(f"  신규 등록 공연: {new_count}건" + ("  (첫 실행이라 표시 안 함)" if not prev_ids else ""))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fp:
        json.dump({
            "generated_at": today.isoformat(),
            "facilities": facilities,
            "ticket_opens": opens,
            "performances": performances,
        }, fp, ensure_ascii=False, indent=2)
    print(f"저장 완료: {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except KopisUnreachable as e:
        print(f"오류 방지: {e}. 수집을 멈추고 기존 데이터를 그대로 둡니다.", file=sys.stderr)
        sys.exit(1)

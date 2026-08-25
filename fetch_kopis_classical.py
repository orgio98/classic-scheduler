"""
클래식 공연 통합 사이트 - KOPIS 데이터 파이프라인

세종문화회관 / 예술의전당 / 고양아람누리 세 기관의 클래식(서양음악)·무용 공연을
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
from urllib.request import urlopen

API_BASE = "http://www.kopis.or.kr/openApi/restful"
SERVICE_KEY = os.environ.get("KOPIS_API_KEY", "").strip()

VENUES = [
    {"name": "세종문화회관", "keywords": ["세종문화회관", "세종대극장", "세종체임버홀", "세종S씨어터"]},
    {"name": "예술의전당", "keywords": ["예술의전당"]},
    {"name": "고양아람누리", "keywords": ["아람누리", "고양아람"]},
]

# 타 지역 동명 시설 제외 (예: 부산 영화의전당)
EXCLUDE_KEYWORDS = ["부산", "대구", "광주", "대전", "울산", "청주", "전주", "김해", "안산", "성남", "제주"]

LOOKAHEAD_DAYS = 180

GENRE_ORCHESTRA = "오케스트라"
GENRE_OPERA = "오페라"
GENRE_DANCE = "발레·무용"
GENRE_CHAMBER = "실내악·독주"
GENRE_CHOIR = "합창"

OUTPUT_PATH = Path("data/performances.json")


def _get(path: str, **params) -> ET.Element:
    if not SERVICE_KEY:
        print("오류: KOPIS_API_KEY 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)
    params = {"service": SERVICE_KEY, **params}
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    with urlopen(url, timeout=20) as res:
        body = res.read()
    return ET.fromstring(body)


def matches_venue(fcltynm: str, venue: dict) -> bool:
    if not fcltynm:
        return False
    if not any(k in fcltynm for k in venue["keywords"]):
        return False
    if any(x in fcltynm for x in EXCLUDE_KEYWORDS):
        return False
    return True


def find_facilities(venue: dict) -> list[dict]:
    facilities = []
    seen = set()
    for param in ("shprfnmfct", "fcltynm"):
        try:
            root = _get("prfplc", cpage="1", rows="100", **{param: venue["name"]})
        except Exception as e:
            print(f"  경고: 시설 검색 실패 ({param}): {e}", file=sys.stderr)
            continue
        for db in root.findall("db"):
            mt10id = (db.findtext("mt10id") or "").strip()
            fcltynm = (db.findtext("fcltynm") or "").strip()
            if not mt10id or mt10id in seen:
                continue
            if not matches_venue(fcltynm, venue):
                continue
            seen.add(mt10id)
            facilities.append({"mt10id": mt10id, "fcltynm": fcltynm, "venue": venue["name"]})
        if facilities:
            break
        time.sleep(0.2)
    return facilities


def find_performance_ids(mt10id: str, shcate: str, stdate: str, eddate: str) -> list[str]:
    ids = []
    page = 1
    while True:
        try:
            root = _get("pblprfr", stdate=stdate, eddate=eddate, cpage=str(page),
                        rows="100", prfplccd=mt10id, shcate=shcate)
        except Exception as e:
            print(f"  경고: 공연목록 조회 실패 ({mt10id}): {e}", file=sys.stderr)
            break
        dbs = root.findall("db")
        if not dbs:
            break
        for db in dbs:
            mt20id = (db.findtext("mt20id") or "").strip()
            if mt20id:
                ids.append(mt20id)
        if len(dbs) < 100:
            break
        page += 1
        time.sleep(0.2)
    return ids


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


def fetch_detail(mt20id: str) -> dict:
    try:
        root = _get(f"pblprfr/{mt20id}")
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

    return {
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


def guess_genre(perf: dict, shcate: str) -> tuple[str, bool]:
    if shcate == "EEEA":
        return GENRE_DANCE, False
    hay = f"{perf.get('name', '')} {perf.get('genre_raw', '')}"
    if "오페라" in hay:
        return GENRE_OPERA, False
    if "합창" in hay or "칸타타" in hay:
        return GENRE_CHOIR, False
    if any(k in hay for k in ("오케스트라", "필하모닉", "교향악단", "심포니", "관현악")):
        return GENRE_ORCHESTRA, False
    return GENRE_CHAMBER, True


def main():
    today = date.today()
    stdate = today.strftime("%Y%m%d")
    eddate = (today + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y%m%d")

    print(f"[{today}] 공연장 코드 조회 중...")
    facilities = []
    for venue in VENUES:
        found = find_facilities(venue)
        print(f"  {venue['name']}: {len(found)}개 공연장")
        for f in found:
            print(f"     - {f['fcltynm']} ({f['mt10id']})")
        facilities.extend(found)
        time.sleep(0.2)

    if not facilities:
        print("오류: 공연장을 하나도 찾지 못했습니다. API 키/응답을 확인하세요.", file=sys.stderr)
        sys.exit(1)

    print(f"\n공연 목록 조회 중 ({stdate} ~ {eddate})...")
    results = {}
    for f in facilities:
        for shcate in ("CCCA", "EEEA"):
            for mt20id in find_performance_ids(f["mt10id"], shcate, stdate, eddate):
                if mt20id in results:
                    continue
                detail = fetch_detail(mt20id)
                if not detail:
                    continue
                venue_def = next((v for v in VENUES if v["name"] == f["venue"]), None)
                if venue_def and not matches_venue(detail.get("facility_name", ""), venue_def):
                    print(f"  제외(시설 불일치): {detail.get('name')} @ {detail.get('facility_name')}")
                    continue
                genre, guessed = guess_genre(detail, shcate)
                detail["venue"] = f["venue"]
                detail["genre"] = genre
                detail["genre_guessed"] = guessed
                results[mt20id] = detail
                time.sleep(0.15)

    performances = sorted(results.values(), key=lambda p: p.get("start_date", ""))
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

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fp:
        json.dump({
            "generated_at": today.isoformat(),
            "facilities": facilities,
            "performances": performances,
        }, fp, ensure_ascii=False, indent=2)
    print(f"저장 완료: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

"""
클래식 공연 통합 사이트 - KOPIS 데이터 파이프라인

세종문화회관 / 예술의전당 / 고양아람누리 세 기관의 클래식(서양음악)·무용 공연을
KOPIS API로 가져와서 장르별로 분류한 뒤 data/performances.json 으로 저장한다.

필요 환경변수:
    KOPIS_API_KEY - KOPIS 오픈API 서비스키

사용법:
    KOPIS_API_KEY=xxxx python fetch_kopis_classical.py

[중요] 공연장 필터링에 대해
  KOPIS의 공연시설 검색(prfplc)은 파라미터가 무시되면 전국 시설을 그대로 반환한다.
  실제로 이 때문에 부산 공연이 "세종문화회관"으로 잡히는 버그가 있었다.
  그래서 API 응답을 그대로 믿지 않고, 아래 두 단계로 직접 검증한다:
    1) 시설명(fcltynm)에 기관 키워드가 실제로 들어있는지 확인
    2) 공연 상세의 시설명도 한 번 더 확인 (교차 검증)
"""

import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

API_BASE = "http://www.kopis.or.kr/openApi/restful"
SERVICE_KEY = os.environ.get("KOPIS_API_KEY", "").strip()

# 대상 기관. keywords 중 하나라도 시설명에 들어있어야 그 기관으로 인정한다.
VENUES = [
    {"name": "세종문화회관", "keywords": ["세종문화회관", "세종대극장", "세종체임버홀", "세종М"]},
    {"name": "예술의전당", "keywords": ["예술의전당"]},
    {"name": "고양아람누리", "keywords": ["아람누리", "고양아람"]},
]

# 다른 지역의 동명이칭 시설을 걸러내기 위한 제외 키워드
EXCLUDE_KEYWORDS = ["부산", "대구", "광주", "대전", "울산", "청주", "전주", "김해", "안산", "성남"]

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
    """시설명이 실제로 그 기관 것인지 검증한다."""
    if not fcltynm:
        return False
    if not any(k in fcltynm for k in venue["keywords"]):
        return False
    # 서울/고양 소재가 아닌 동명 시설 제외 (예: 부산 영화의전당)
    if any(x in fcltynm for x in EXCLUDE_KEYWORDS):
        return False
    return True


def find_facilities(venue: dict) -> list[dict]:
    """기관명으로 하위 공연장 목록을 찾고, 이름을 직접 검증해서 걸러낸다."""
    facilities = []
    seen = set()

    # KOPIS 시설검색은 파라미터명이 버전에 따라 다를 수 있어 두 가지를 모두 시도한다.
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
            # ★ API 응답을 믿지 않고 이름으로 직접 검증
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
            root = _get(
                "pblprfr",
                stdate=stdate,
                eddate=eddate,
                cpage=str(page),
                rows="100",
                prfplccd=mt10id,
                shcate=shcate,
            )
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

    return {
        "mt20id": mt20id,
        "name": text("prfnm"),
        "start_date": text("prfpdfrom"),
        "end_date": text("prfpdto"),
        "facility_name": text("fcltynm"),
        "poster": text("poster"),
        "cast": text("prfcast"),
        "crew": text("prfcrew"),
        "runtime": text("prfruntime"),
        "age": text("prfage"),
        "company": text("entrpsnmH"),
        "price": text("pcseguidance"),
        "schedule": text("dtguidance"),  # 요일별 공연시간 안내
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
    venue_lookup = {f["mt10id"]: f for f in facilities}

    for f in facilities:
        for shcate in ("CCCA", "EEEA"):
            for mt20id in find_performance_ids(f["mt10id"], shcate, stdate, eddate):
                if mt20id in results:
                    continue
                detail = fetch_detail(mt20id)
                if not detail:
                    continue

                # ★ 교차 검증: 상세의 시설명도 그 기관이 맞는지 다시 확인
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
    for p in performances:
        by_venue[p["venue"]] = by_venue.get(p["venue"], 0) + 1
    for v, c in by_venue.items():
        print(f"  {v}: {c}건")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fp:
        json.dump(
            {
                "generated_at": today.isoformat(),
                "facilities": facilities,
                "performances": performances,
            },
            fp,
            ensure_ascii=False,
            indent=2,
        )
    print(f"저장 완료: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

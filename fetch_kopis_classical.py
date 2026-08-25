"""
클래식 공연 통합 사이트 - Stage 1: KOPIS 데이터 파이프라인

세종문화회관 / 예술의전당 / 고양아람누리 세 기관의 클래식(서양음악)·무용 공연을
KOPIS(공연예술통합전산망) API로 가져와서 장르별(오케스트라/오페라/발레·무용/
실내악·독주/합창)로 1차 분류한 뒤 data/performances.json 으로 저장한다.

필요 환경변수:
    KOPIS_API_KEY - KOPIS 오픈API 서비스키 (뮤지컬 프로젝트에서 쓰던 키 재사용 가능)

사용법:
    KOPIS_API_KEY=xxxx python fetch_kopis_classical.py

참고:
- KOPIS는 XML로만 응답한다.
- shcate(장르코드): CCCA=서양음악(클래식), EEEA=무용(서양/한국무용)
  (KOPIS 자체에는 오케스트라/오페라/합창처럼 더 세부적인 장르 코드가 없어서,
   공연명 키워드로 2차 분류한다. 오분류는 나중에 웹앱에서 수동으로 고칠 수 있게
   장르 필드를 그냥 데이터값으로 취급하고, genre_guessed=True 플래그를 남겨둔다.)
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

# 조사 대상 3개 기관 (부분일치 검색 - 하위 공연장이 자동으로 다 잡힌다)
VENUES = ["세종문화회관", "예술의전당", "고양아람누리"]

# 조회 기간: 오늘부터 N일 뒤까지
LOOKAHEAD_DAYS = 180

GENRE_ORCHESTRA = "오케스트라"
GENRE_OPERA = "오페라"
GENRE_DANCE = "발레·무용"
GENRE_CHAMBER = "실내악·독주"
GENRE_CHOIR = "합창"

OUTPUT_PATH = Path("data/performances.json")


def _get(path: str, **params) -> ET.Element:
    """KOPIS API GET 요청 후 XML 루트 엘리먼트 반환."""
    if not SERVICE_KEY:
        print("오류: KOPIS_API_KEY 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    params = {"service": SERVICE_KEY, **params}
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    with urlopen(url, timeout=20) as res:
        body = res.read()
    return ET.fromstring(body)


def find_facility_codes(venue_name: str) -> list[dict]:
    """기관명(부분일치)으로 하위 공연장(mt10id) 전체 목록을 찾는다."""
    root = _get("prfplc", fcltynm=venue_name, cpage="1", rows="100")
    facilities = []
    for db in root.findall("db"):
        mt10id = db.findtext("mt10id", "").strip()
        fcltynm = db.findtext("fcltynm", "").strip()
        if mt10id:
            facilities.append({"mt10id": mt10id, "fcltynm": fcltynm, "venue": venue_name})
    return facilities


def find_performances(mt10id: str, shcate: str, stdate: str, eddate: str) -> list[str]:
    """공연장 + 장르코드로 공연목록(mt20id 리스트)을 가져온다. (페이지네이션 처리)"""
    mt20ids = []
    page = 1
    while True:
        root = _get(
            "pblprfr",
            stdate=stdate,
            eddate=eddate,
            cpage=str(page),
            rows="100",
            prfplccd=mt10id,
            shcate=shcate,
        )
        dbs = root.findall("db")
        if not dbs:
            break
        for db in dbs:
            mt20id = db.findtext("mt20id", "").strip()
            if mt20id:
                mt20ids.append(mt20id)
        if len(dbs) < 100:
            break
        page += 1
        time.sleep(0.2)
    return mt20ids


def fetch_detail(mt20id: str) -> dict:
    """공연 상세정보(출연진·기간·장소·가격 등)를 가져온다."""
    root = _get(f"pblprfr/{mt20id}")
    db = root.find("db")
    if db is None:
        return {}

    def text(tag, default=""):
        return (db.findtext(tag) or default).strip()

    return {
        "mt20id": mt20id,
        "name": text("prfnm"),
        "start_date": text("prfpdfrom"),
        "end_date": text("prfpdto"),
        "facility_name": text("fcltynm"),
        "poster": text("poster"),
        "cast": text("prfcast"),  # 출연진 (지휘자·협연자 등, 자유텍스트)
        "crew": text("prfcrew"),  # 제작진
        "runtime": text("prfruntime"),
        "age": text("prfage"),
        "company": text("entrpsnmH"),
        "price": text("pcseguidance"),
        "synopsis": text("sty"),
        "genre_raw": text("genrenm"),
        "state": text("prfstate"),  # 공연중/공연예정/공연완료
    }


def guess_genre(perf: dict, shcate: str) -> tuple[str, bool]:
    """공연명/장르 키워드로 5개 카테고리 중 하나로 분류한다. (오분류 가능 -> 수동 보정 대상)"""
    if shcate == "EEEA":
        return GENRE_DANCE, False

    name = perf.get("name", "")
    haystack = f"{name} {perf.get('genre_raw', '')}"

    if "오페라" in haystack:
        return GENRE_OPERA, False
    if "합창" in haystack:
        return GENRE_CHOIR, False
    if any(k in haystack for k in ("오케스트라", "필하모닉", "교향악단", "심포니")):
        return GENRE_ORCHESTRA, False
    # 나머지는 실내악·독주로 기본 분류 (불확실하므로 guessed=True)
    return GENRE_CHAMBER, True


def collect_facilities() -> list[dict]:
    all_facilities = []
    seen = set()
    for venue in VENUES:
        for f in find_facility_codes(venue):
            if f["mt10id"] in seen:
                continue
            seen.add(f["mt10id"])
            all_facilities.append(f)
        time.sleep(0.2)
    return all_facilities


def collect_performances(facilities: list[dict], stdate: str, eddate: str) -> list[dict]:
    results = {}
    for f in facilities:
        for shcate in ("CCCA", "EEEA"):
            mt20ids = find_performances(f["mt10id"], shcate, stdate, eddate)
            for mt20id in mt20ids:
                if mt20id in results:
                    continue
                detail = fetch_detail(mt20id)
                if not detail:
                    continue
                genre, guessed = guess_genre(detail, shcate)
                detail["venue"] = f["venue"]
                detail["genre"] = genre
                detail["genre_guessed"] = guessed
                results[mt20id] = detail
                time.sleep(0.15)
    return list(results.values())


def main():
    today = date.today()
    stdate = today.strftime("%Y%m%d")
    eddate = (today + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y%m%d")

    print(f"[{today}] 공연장 코드 조회 중...")
    facilities = collect_facilities()
    print(f"  -> {len(facilities)}개 공연장 발견")
    for f in facilities:
        print(f"     {f['venue']} | {f['fcltynm']} ({f['mt10id']})")

    print(f"공연 목록 조회 중 ({stdate} ~ {eddate})...")
    performances = collect_performances(facilities, stdate, eddate)
    print(f"  -> {len(performances)}건 수집")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": today.isoformat(),
                "facilities": facilities,
                "performances": performances,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"저장 완료: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

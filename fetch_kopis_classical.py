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

from fetch_ticket_open import collect_ticket_opens, match_ticket_opens

API_BASE = "http://www.kopis.or.kr/openApi/restful"
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
        "label": "세종예술의전당",          # 세종특별자치시 소재
        "prefix": ["세종예술의전당"],
    },
    {
        "label": "예술의전당",              # 서울 서초구 소재
        "prefix": ["예술의전당"],
    },
    {
        "label": "고양아람누리",
        "prefix": ["고양아람누리", "아람누리"],
        "contains": ["아람음악당", "아람극장"],
    },
]

# KOPIS 시설 검색에 사용할 키워드
SEARCH_TERMS = ["세종문화회관", "예술의전당", "세종예술의전당", "고양아람누리"]

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


def find_facilities() -> list[dict]:
    """
    SEARCH_TERMS 로 KOPIS 시설을 검색하고, 시설명으로 기관을 판정해서 목록을 만든다.
    검색 결과를 그대로 믿지 않고 resolve_venue() 로 직접 걸러낸다.
    """
    facilities = []
    seen = set()

    for term in SEARCH_TERMS:
        got = False
        for param in ("shprfnmfct", "fcltynm"):
            try:
                root = _get("prfplc", cpage="1", rows="100", **{param: term})
            except Exception as ex:
                print(f"  경고: 시설 검색 실패 ({term}/{param}): {ex}", file=sys.stderr)
                continue

            for db in root.findall("db"):
                mt10id = (db.findtext("mt10id") or "").strip()
                fcltynm = (db.findtext("fcltynm") or "").strip()
                if not mt10id or mt10id in seen:
                    continue
                label = resolve_venue(fcltynm)
                if label is None:
                    continue
                seen.add(mt10id)
                facilities.append({"mt10id": mt10id, "fcltynm": fcltynm, "venue": label})
                got = True

            if got:
                break
            time.sleep(0.2)
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
                # 교차 검증: 상세의 시설명으로 다시 판정한다.
                # 목록 API가 엉뚱한 공연을 섞어 보내는 경우를 여기서 최종적으로 걸러낸다.
                label = resolve_venue(detail.get("facility_name", ""))
                if label is None:
                    print(f"  제외(대상 아님): {detail.get('name')} @ {detail.get('facility_name')}")
                    continue
                genre, guessed = guess_genre(detail, shcate)
                detail["venue"] = label   # 검색어가 아니라 실제 시설명 기준
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

    # ── 티켓오픈 공지 수집 후 공연에 매칭 ──────────────────────────────
    print("\n티켓오픈 공지 수집 중...")
    opens = collect_ticket_opens()
    matched = match_ticket_opens(performances, opens)
    print(f"  공연과 매칭된 티켓오픈 공지: {matched}건")

    # ── 신규 공연 감지 ────────────────────────────────────────────────
    # 지난 실행 결과와 비교해서 이번에 새로 등장한 공연을 표시한다.
    # KOPIS에 공연이 올라오는 시점은 대체로 예매 오픈과 가까워서,
    # 티켓오픈 공지가 없는 공연장(예술의전당 등)에도 NEW 표시가 붙는다.
    prev_ids = set()
    if OUTPUT_PATH.exists():
        try:
            with OUTPUT_PATH.open(encoding="utf-8") as fp:
                prev = json.load(fp)
            prev_ids = {p.get("mt20id") for p in prev.get("performances", [])}
        except Exception as e:
            print(f"  경고: 이전 데이터 읽기 실패(첫 실행이면 정상): {e}", file=sys.stderr)

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
    main()

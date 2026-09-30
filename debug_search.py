"""
진단 도구: 특정 공연이 왜 목록에 안 잡히는지 확인한다.

메인 파이프라인(fetch_kopis_classical.py)은 우리가 지정한 공연장(mt10id)과
장르코드(CCCA/EEEA)로만 조회한다. 그래서 공연이 실제로는 KOPIS에 있는데도
다음 세 가지 이유 중 하나로 빠질 수 있다:

  1. 공연장 명칭이 우리 VENUE_RULES와 다르게 등록됨
     (예: "고양아람누리"가 아니라 "고양문화재단"으로 등록)
  2. 장르가 CCCA(서양음악 클래식)/EEEA(무용)가 아닌 다른 코드로 등록됨
  3. KOPIS에 아직 등록 자체가 안 됨

이 스크립트는 공연장·장르 필터를 걸지 않고 "공연명"으로만 KOPIS 전체를
검색해서, 실제로 어떤 시설명·장르로 등록돼 있는지 그대로 보여준다.
그러면 위 세 가지 중 정확히 무엇이 원인인지 바로 알 수 있다.

사용법 (GitHub Actions "Debug search" 워크플로우에서 keyword 입력 후 실행):
    KOPIS_API_KEY=xxxx DEBUG_KEYWORD="손열음" python debug_search.py
"""

import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

API_BASE = "http://www.kopis.or.kr/openApi/restful"
SERVICE_KEY = os.environ.get("KOPIS_API_KEY", "").strip()
KEYWORD = os.environ.get("DEBUG_KEYWORD", "").strip()

# 메인 파이프라인이 실제로 쓰는 판정 규칙을 그대로 가져와서 비교한다.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from fetch_kopis_classical import resolve_venue, VENUE_RULES
except Exception:
    resolve_venue = None
    VENUE_RULES = []

# 우리가 실제로 쓰는 장르코드 (fetch_kopis_classical.py 와 동일하게 유지할 것)
OUR_GENRE_CODES = {"CCCA": "서양음악(클래식)", "EEEA": "무용"}


def _get(path: str, **params) -> ET.Element:
    params = {"service": SERVICE_KEY, **params}
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=20) as res:
            body = res.read()
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace").strip()[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason}" + (f" | 응답: {detail}" if detail else "")) from None
    except URLError as e:
        raise RuntimeError(f"네트워크 오류: {e.reason}") from None
    return ET.fromstring(body)


def search_by_title(keyword: str, stdate: str, eddate: str) -> list[dict]:
    """공연장·장르 필터 없이 공연명으로만 전체 검색한다."""
    results = []
    page = 1
    while True:
        root = _get(
            "pblprfr",
            stdate=stdate, eddate=eddate,
            cpage=str(page), rows="100",
            shprfnm=keyword,          # 공연명 검색 (극장명 검색은 shprfnmfct)
        )
        dbs = root.findall("db")
        if not dbs:
            break
        for db in dbs:
            results.append({
                "mt20id": (db.findtext("mt20id") or "").strip(),
                "name": (db.findtext("prfnm") or "").strip(),
                "facility": (db.findtext("fcltynm") or "").strip(),
                "start": (db.findtext("prfpdfrom") or "").strip(),
                "end": (db.findtext("prfpdto") or "").strip(),
                "genre": (db.findtext("genrenm") or "").strip(),
            })
        if len(dbs) < 100:
            break
        page += 1
        time.sleep(0.2)
    return results


def main():
    if not SERVICE_KEY:
        print("오류: KOPIS_API_KEY 가 없습니다.", file=sys.stderr)
        sys.exit(1)
    if not KEYWORD:
        print("오류: DEBUG_KEYWORD 가 비어 있습니다. (Actions 실행 시 keyword 입력)", file=sys.stderr)
        sys.exit(1)

    today = date.today()
    stdate = today.strftime("%Y%m%d")
    # 진단용이므로 메인 파이프라인(180일)보다 넉넉하게 400일을 본다.
    eddate = (today + timedelta(days=400)).strftime("%Y%m%d")

    print(f"'{KEYWORD}' 로 KOPIS 전체 검색 ({stdate} ~ {eddate}, 장르·공연장 필터 없음)\n")

    try:
        results = search_by_title(KEYWORD, stdate, eddate)
    except Exception as e:
        print(f"검색 실패: {e}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("=> KOPIS에 이 키워드로 검색되는 공연이 없습니다.")
        print("   (원인 ③: KOPIS에 아직 등록 자체가 안 됐을 가능성이 높습니다.")
        print("    공연 자체는 다른 예매처에 있어도, KOPIS 미등록이면 이 앱에는")
        print("    반영할 방법이 없습니다.)")
        return

    print(f"=> {len(results)}건 발견\n")

    for r in results:
        print(f"[{r['mt20id']}] {r['name']}")
        print(f"    공연장(fcltynm 원문): {r['facility']!r}")
        print(f"    기간: {r['start']} ~ {r['end']}")
        print(f"    genrenm(참고용): {r['genre']!r}")

        # ① 공연장 판정 — 우리 VENUE_RULES 로 실제 인식되는지
        if resolve_venue:
            venue = resolve_venue(r["facility"])
            if venue:
                print(f"    -> 공연장 판정 결과: '{venue}' 로 인식됨 (원인 ① 아님)")
            else:
                print(f"    -> ⚠ 공연장 판정 결과: 어디에도 안 걸림!")
                print(f"       (원인 ①: VENUE_RULES 에 '{r['facility']}' 를 매칭하는 규칙이 없습니다)")

        # ② 장르 코드는 상세조회를 해야 정확한 shcate 를 알 수 있어서,
        #    여기서는 실제로 우리 조회(shcate=CCCA/EEEA)에 이 mt20id 가 걸리는지
        #    직접 재현해서 확인한다.
        found_in_our_query = False
        for code in OUR_GENRE_CODES:
            try:
                root = _get(
                    "pblprfr", stdate=stdate, eddate=eddate,
                    cpage="1", rows="100", shcate=code, shprfnm=KEYWORD,
                )
                ids = [(db.findtext("mt20id") or "").strip() for db in root.findall("db")]
                if r["mt20id"] in ids:
                    found_in_our_query = True
                    print(f"    -> shcate={code}({OUR_GENRE_CODES[code]}) 조회에 포함됨 (원인 ② 아님)")
            except Exception as e:
                print(f"    -> shcate={code} 재조회 실패: {e}")
            time.sleep(0.15)

        if not found_in_our_query:
            print(f"    -> ⚠ 우리가 쓰는 장르코드(CCCA/EEEA) 조회 어디에도 안 걸림!")
            print(f"       (원인 ②: 다른 장르코드로 등록되어 있을 가능성이 높습니다)")

        print()


if __name__ == "__main__":
    main()

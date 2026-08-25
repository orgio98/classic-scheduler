"""
티켓오픈 공지 수집 모듈

공연장 공지 게시판에서 "티켓 오픈 안내" 글을 긁어와, 공연명과 매칭해서
각 공연에 티켓오픈일 정보를 붙인다.

[크롤링 가능 여부 - 2026.08 확인 기준]
  ✅ 세종문화회관  : 티켓오픈안내 게시판이 서버 렌더링 HTML 표.
                    제목 / 작성일 / 티켓오픈일 / D-day 컬럼이 그대로 들어있어 안정적.
  ✅ 예술의전당    : 화면은 JS로 그려지지만, 내부적으로 dataTicketList 가 JSON을
                    그대로 내려준다. 공연명·티켓오픈일·공연장·가격·장르까지 포함.
  ❓ 고양아람누리 / 세종예술의전당 : 게시판 구조 미확인.

한 곳이 실패해도 나머지는 정상 동작하도록 각 수집기를 개별적으로 감싼다.
"""

import json
import re
import sys
import time
from html import unescape
from urllib.request import Request, urlopen

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SEJONG_LIST_URL = (
    "https://www.sejongpac.or.kr/portal/bbs/B0000049/list.do"
    "?menuNo=200440&pageIndex={page}"
)
SEJONG_VIEW_URL = (
    "https://www.sejongpac.or.kr/portal/bbs/B0000049/view.do"
    "?nttId={ntt}&menuNo=200440"
)

# 예술의전당 선예매 티켓오픈 목록 API (JSON).
# 페이지 화면은 JS로 그려지지만, 실제 데이터는 이 주소에서 JSON으로 내려온다.
SAC_LIST_URL = (
    "https://www.sac.or.kr/site/main/show/dataTicketList"
    "?cp={page}&pageSize=30&ticketOpenFlag=Y"
    "&sortOrder=B.TICKET_OPEN_DATE&sortDirection=DESC"
)
SAC_VIEW_URL = "https://www.sac.or.kr/site/main/show/show_view?SN={sn}"
SAC_LIST_PAGE = "https://www.sac.or.kr/site/main/ticketopen/ticketopen_list"

# 몇 페이지까지 훑을지 (1페이지 10건 정도)
MAX_PAGES = 3


def _fetch(url: str) -> str:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=20) as res:
        raw = res.read()
    return raw.decode("utf-8", errors="replace")


def _strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").strip()


def fetch_sejong_ticket_opens() -> list[dict]:
    """
    세종문화회관 티켓오픈안내 게시판을 긁는다.

    표의 각 행 구조:
      No. | 제목(링크: view.do?nttId=…) | 작성일 | 티켓오픈일 | 디데이 | 조회수
    """
    items = []
    seen = set()

    for page in range(1, MAX_PAGES + 1):
        try:
            html = _fetch(SEJONG_LIST_URL.format(page=page))
        except Exception as e:
            print(f"  경고: 세종문화회관 티켓오픈 {page}페이지 실패: {e}", file=sys.stderr)
            break

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        found_in_page = 0

        for row in rows:
            m = re.search(r"view\.do\?nttId=(\d+)", row)
            if not m:
                continue
            ntt = m.group(1)
            if ntt in seen:
                continue

            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            if len(cells) < 4:
                continue
            texts = [_strip_tags(c) for c in cells]

            # 제목: 링크 텍스트에서 뽑는다
            tm = re.search(r"view\.do\?nttId=\d+[^\"']*[\"'][^>]*>(.*?)</a>", row, re.S)
            title = _strip_tags(tm.group(1)) if tm else ""
            if not title:
                continue

            # 날짜 후보를 모두 찾는다. 시각까지 있는 것이 '티켓오픈일'.
            dates_with_time = re.findall(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", " | ".join(texts))
            dates_only = re.findall(r"\d{4}-\d{2}-\d{2}(?!\s+\d{2}:)", " | ".join(texts))

            open_at = dates_with_time[0] if dates_with_time else (
                dates_only[1] if len(dates_only) > 1 else None
            )
            posted_at = dates_only[0] if dates_only else None

            # 디데이 컬럼 ("D-2" / "종료")
            dday = next((t for t in texts if re.fullmatch(r"D-\d+|D-DAY|종료", t.strip(), re.I)), None)

            seen.add(ntt)
            found_in_page += 1
            items.append({
                "venue": "세종문화회관",
                "title": title,
                "open_at": open_at,        # "2026-08-27 14:00"
                "posted_at": posted_at,    # "2026-08-20"
                "dday": dday,              # "D-2" / "종료"
                "url": SEJONG_VIEW_URL.format(ntt=ntt),
                "source": "세종문화회관 티켓오픈안내",
            })

        if found_in_page == 0:
            break
        time.sleep(0.3)

    return items


def fetch_sac_ticket_opens() -> list[dict]:
    """
    예술의전당 선예매 티켓오픈 목록.

    dataTicketList 가 JSON을 그대로 내려준다. 응답 구조:
        { "result": "success",
          "paging": { "totalPage": 2, "result": [ {공연1}, {공연2}, ... ] } }

    각 항목에서 쓰는 필드:
        PROGRAM_SUBJECT        공연명
        TICKET_OPEN_DATE       티켓오픈일 (YYYY-MM-DD)
        PLACE_NAME             공연장 (콘서트홀 / IBK기업은행챔버홀 …)
        CATEGORY_SECONDARY_NAME 장르 (클래식 …)
        SALE_STATE_CODE_NAME   상태 (예정 / 예매)
        PRICE_INFO             가격 안내
        SN                     상세 페이지 번호
    """
    items = []
    page = 1
    total_page = 1

    while page <= total_page and page <= MAX_PAGES:
        try:
            body = _fetch(SAC_LIST_URL.format(page=page))
            data = json.loads(body)
        except Exception as e:
            print(f"  경고: 예술의전당 티켓오픈 {page}페이지 실패: {e}", file=sys.stderr)
            break

        paging = data.get("paging") or {}
        rows = paging.get("result")
        if not isinstance(rows, list):
            # 구조가 바뀌었을 때를 대비한 대체 경로
            rows = data.get("result") if isinstance(data.get("result"), list) else []

        try:
            total_page = int(paging.get("totalPage") or 1)
        except (TypeError, ValueError):
            total_page = 1

        for r in rows:
            title = (r.get("PROGRAM_SUBJECT") or "").strip()
            open_at = (r.get("TICKET_OPEN_DATE") or "").strip()
            if not title:
                continue
            sn = r.get("SN")
            items.append({
                "venue": "예술의전당",
                "title": title,
                "open_at": open_at or None,
                "posted_at": None,
                "dday": None,                     # 날짜만 있으므로 화면에서 계산
                "hall": (r.get("PLACE_NAME") or "").strip() or None,
                "state": (r.get("SALE_STATE_CODE_NAME") or "").strip() or None,
                "price_info": (r.get("PRICE_INFO") or "").strip() or None,
                "url": SAC_VIEW_URL.format(sn=sn) if sn else SAC_LIST_PAGE,
                "source": "예술의전당 선예매 티켓오픈공지",
            })

        page += 1
        time.sleep(0.3)

    return items


# ── 공연명 매칭 ───────────────────────────────────────────────────────────

_NOISE = re.compile(
    r"(티켓\s*오픈\s*안내|추가좌석\s*오픈\s*안내|마지막\s*티켓\s*오픈|"
    r"\d+차\s*티켓\s*오픈|프리뷰\s*티켓\s*오픈|선예매|오픈\s*안내)"
)


def normalize_title(s: str) -> str:
    """
    비교용으로 제목을 단순화한다.

    같은 공연이라도 KOPIS와 공연장 사이트의 표기가 미묘하게 다르다:
      KOPIS  : 2026 서울국제음악제 '이안 보스트리지: 슈만 시인의 사랑'
      예술의전당: 2026 서울국제음악제  ‘이안 보스트리지: 슈만 시인의 사랑’
    작은따옴표 종류(' vs ‘’)와 공백 개수가 다르므로, 기호를 전부 없애고
    공백도 제거한 뒤에 비교해야 매칭된다.
    """
    s = _NOISE.sub(" ", s or "")
    # 한글/영문/숫자만 남기고 나머지(따옴표·괄호·기호·공백)는 전부 제거
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", s)
    return s.lower()


def match_ticket_opens(performances: list[dict], opens: list[dict]) -> int:
    """
    티켓오픈 공지를 공연에 붙인다.
    공지 제목에는 "○○ 티켓 오픈 안내" 처럼 군더더기가 붙으므로 정규화 후
    서로 포함 관계인지로 판정한다. (완전일치만 보면 거의 안 잡힌다)
    """
    prepared = []
    for o in opens:
        key = normalize_title(o["title"])
        if len(key) >= 3:
            prepared.append((key, o))

    matched = 0
    for p in performances:
        pkey = normalize_title(p.get("name", ""))
        if len(pkey) < 3:
            continue
        best = None
        for key, o in prepared:
            # 같은 공연장 공지가 우선. 서로 포함되면 같은 공연으로 본다.
            if pkey in key or key in pkey:
                if o["venue"] == p.get("venue"):
                    best = o
                    break
                if best is None:
                    best = o
        if best:
            p["ticket_open"] = {
                "open_at": best["open_at"],
                "dday": best["dday"],
                "url": best["url"],
                "source": best["source"],
            }
            matched += 1
    return matched


def collect_ticket_opens() -> list[dict]:
    opens = []
    for fn in (fetch_sejong_ticket_opens, fetch_sac_ticket_opens):
        try:
            got = fn()
            opens.extend(got)
            print(f"  {fn.__name__}: {len(got)}건")
        except Exception as e:
            print(f"  경고: {fn.__name__} 실패: {e}", file=sys.stderr)
    return opens

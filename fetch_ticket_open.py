"""
티켓오픈 공지 수집 모듈

공연장 공지 게시판에서 "티켓 오픈 안내" 글을 긁어와, 공연명과 매칭해서
각 공연에 티켓오픈일 정보를 붙인다.

[크롤링 가능 여부 - 2026.08 확인 기준]
  ✅ 세종문화회관  : 티켓오픈안내 게시판이 서버 렌더링 HTML 표.
                    제목 / 작성일 / 티켓오픈일 / D-day 컬럼이 그대로 들어있어 안정적.
  ❌ 예술의전당    : 티켓오픈공지 페이지가 자바스크립트로 목록을 나중에 불러오는 구조.
                    HTML만 받으면 "티켓 리스트 로딩중"만 나오고 데이터가 없다.
                    → 내부 AJAX 주소를 알아내야 가능. 브라우저 개발자도구
                      (F12 → Network 탭)에서 목록 요청 URL을 찾아 SAC_LIST_URL에
                      넣으면 활성화된다.
  ❓ 고양아람누리 / 세종예술의전당 : 게시판 구조 미확인.

한 곳이 실패해도 나머지는 정상 동작하도록 각 수집기를 개별적으로 감싼다.
"""

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

# 예술의전당 티켓오픈 목록의 실제 데이터 주소를 알아내면 여기에 넣는다.
# (브라우저 F12 → Network 탭에서 ticketopen 목록을 불러오는 요청 URL)
SAC_LIST_URL = None

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
    예술의전당 선예매 티켓오픈공지.
    목록이 자바스크립트로 로딩되므로 SAC_LIST_URL 이 설정된 경우에만 동작한다.
    """
    if not SAC_LIST_URL:
        print("  안내: 예술의전당 티켓오픈은 JS 로딩 구조라 건너뜁니다 "
              "(SAC_LIST_URL 설정 시 활성화)")
        return []
    try:
        html = _fetch(SAC_LIST_URL)
    except Exception as e:
        print(f"  경고: 예술의전당 티켓오픈 수집 실패: {e}", file=sys.stderr)
        return []

    items = []
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", html, re.S | re.I):
        block = m.group(1)
        title = _strip_tags(block)
        if not title:
            continue
        dm = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", block)
        open_at = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else None
        items.append({
            "venue": "예술의전당",
            "title": title[:120],
            "open_at": open_at,
            "posted_at": None,
            "dday": None,
            "url": "https://www.sac.or.kr/site/main/ticketopen/ticketopen_list",
            "source": "예술의전당 선예매 티켓오픈공지",
        })
    return items


# ── 공연명 매칭 ───────────────────────────────────────────────────────────

_NOISE = re.compile(
    r"(티켓\s*오픈\s*안내|추가좌석\s*오픈\s*안내|마지막\s*티켓\s*오픈|"
    r"\d+차\s*티켓\s*오픈|프리뷰\s*티켓\s*오픈|선예매|오픈\s*안내)"
)


def normalize_title(s: str) -> str:
    """비교용으로 제목을 단순화한다 (공지 문구·괄호·기호·공백 제거)."""
    s = _NOISE.sub(" ", s or "")
    s = re.sub(r"[\[\]<>〈〉《》（）()「」『』:：·,，\-–—_\"'’“”]", " ", s)
    s = re.sub(r"\s+", "", s)
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

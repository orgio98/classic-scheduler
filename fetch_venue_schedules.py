"""
공연장 자체 일정 보완 수집기

대상:
  - 롯데콘서트홀
  - 고양아람누리

원칙:
  1. 공연장 공식 사이트의 공연명/일정/공연장/원문 URL만 수집한다.
  2. KOPIS와 중복되는 공연은 index.html에서 KOPIS를 우선한다.
  3. 공식 사이트가 일시적으로 차단/타임아웃되면 기존 venue_schedules.json을 보존한다.
  4. 고양아람누리는 공식 메인 공연목록(MA0001v.aspx)을 사용한다.
"""

import html
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

OUT = Path("data/venue_schedules.json")
LOOKAHEAD_DAYS = 180
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/154.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

LOTTE_LIST = "https://m.lotteconcerthall.com/kor/performance"
GOYANG_LIST = "https://www.artgy.or.kr/MA/MA0001v.aspx"


def fetch(url):
    """urllib -> curl 순으로 시도한다. HTML 사이트의 TLS 특이성을 완화한다."""
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "ignore")
    except Exception as first:
        # GitHub-hosted runner에서 일부 사이트가 urllib TLS를 끊는 경우 curl 재시도.
        try:
            cp = subprocess.run(
                [
                    "curl", "-L", "--http1.1", "--compressed",
                    "--connect-timeout", "15", "--max-time", str(TIMEOUT),
                    "-A", HEADERS["User-Agent"], "-sS", url
                ],
                capture_output=True, text=True, encoding="utf-8",
                errors="ignore", timeout=TIMEOUT + 5
            )
            if cp.returncode == 0 and cp.stdout.strip():
                return cp.stdout
        except Exception:
            pass
        raise first


def clean_text(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def norm_title(s):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


def parse_date_range(text):
    vals = []
    for m in re.finditer(
        r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})",
        text or ""
    ):
        try:
            vals.append(
                f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
            )
        except ValueError:
            pass
    return sorted(set(vals))


def in_range(start, end, lo, hi):
    if not start:
        return False
    end = end or start
    return not (end < lo or start > hi)


def guess_genre(title, raw=""):
    t = f"{title} {raw}".lower()
    if any(x in t for x in ("발레", "무용", "댄스", "dance")):
        return "발레·무용"
    if any(x in t for x in ("오페라", "opera")):
        return "오페라"
    if any(x in t for x in ("합창", "choral", "choir")):
        return "합창"
    if any(x in t for x in (
        "교향", "필하모닉", "오케스트라", "심포니", "클래식",
        "피아노", "바이올린", "첼로", "실내악", "리사이틀",
        "콘체르토", "협주곡", "quartet"
    )):
        return "클래식"
    return "공연"


class LinkParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links = []
        self._a = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        d = dict(attrs)
        self._a = d
        self._parts = []

    def handle_data(self, data):
        if self._a is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._a is not None:
            href = self._a.get("href", "")
            if href:
                self.links.append((
                    urljoin(self.base, href),
                    clean_text(" ".join(self._parts))
                ))
            self._a = None
            self._parts = []


def extract_links(url):
    text = fetch(url)
    p = LinkParser(url)
    p.feed(text)
    return p.links


def strip_tags(s):
    return clean_text(re.sub(r"<[^>]+>", " ", s or ""))


def extract_time(text):
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text or "")
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"(오전|오후)\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text or "")
    if m:
        h = int(m.group(2))
        minute = int(m.group(3) or 0)
        if m.group(1) == "오후" and h != 12:
            h += 12
        if m.group(1) == "오전" and h == 12:
            h = 0
        return f"{h:02d}:{minute:02d}"
    return ""


def make_item(venue, title, start, end, hall, url, raw=""):
    genre = guess_genre(title, raw)
    # 공연장 자체 일정은 상세 장르를 그대로 매핑할 수 없으므로
    # 화면의 6개 장르에 맞추기 어려운 일반 콘서트 등은 실내악·독주로
    # 임의 분류하지 않는다. 제목에 명확한 키워드가 있을 때만 분류한다.
    if genre == "공연":
        genre = "실내악·독주"
        guessed = True
    elif genre == "클래식":
        genre = "실내악·독주"
        guessed = True
    else:
        guessed = True

    return {
        "id": f"{venue}:{start}:{norm_title(title)}",
        "venue": venue,
        "facility_name": hall or venue,
        "name": title,
        "start_date": start,
        "end_date": end or start,
        "specific_dates": [],
        "time": extract_time(raw),
        "genre": genre,
        "genre_guessed": guessed,
        "source": venue,
        "source_type": "venue_schedule",
        "source_url": url,
        "booking_links": [{"name": venue, "url": url}],
        "is_kopis": False,
    }


def collect_goyang(lo, hi):
    items = {}
    links = extract_links(GOYANG_LIST)

    # MA0001v.aspx는 현재 공개 중인 주요 공연을 서버 HTML에 직접 노출한다.
    # 상세 URL은 PF0201V.aspx?showid=XXXXXXXX 형식이다.
    details = []
    for href, label in links:
        if "/PF/PF0201V.aspx" in href:
            details.append((href, label))

    print(f"[고양아람누리] 목록 상세링크 {len(details)}건")

    for href, label in details:
        try:
            text_html = fetch(href)
        except Exception as e:
            print(f"[WARN] 고양 상세 실패: {href} / {e}", file=sys.stderr)
            continue

        text = strip_tags(text_html)
        dates = parse_date_range(text)
        if not dates:
            continue

        start, end = dates[0], dates[-1]
        if not in_range(start, end, lo, hi):
            continue

        # 어울림누리는 제외하고 아람누리만 수집.
        if "고양아람누리" not in text and "아람누리" not in text:
            continue
        if "고양어울림누리" in text and "고양아람누리" not in text:
            continue

        title = clean_text(label)
        if not title:
            m = re.search(r"# 공연정보\s+(.+?)\s+(?:\d+ 명이 추천했어요!|장르/테마)", text)
            title = clean_text(m.group(1)) if m else ""
        if not title:
            continue

        mh = re.search(r"공연장소\s*\|\s*(.+?)(?:\s+공연장정보|\s+공연일정)", text)
        hall = clean_text(mh.group(1)) if mh else "고양아람누리"

        item = make_item("고양아람누리", title, start, end, hall, href, text)
        items[item["id"]] = item

    return list(items.values())


def collect_lotte(lo, hi):
    """
    롯데콘서트홀 공식 페이지는 현재 GitHub Actions/일부 자동화 환경에서
    TLS/WAF 차단이 발생할 수 있다. 성공하면 상세 링크를 수집하고,
    실패하면 빈 목록을 반환하여 기존 JSON을 보존하도록 한다.
    """
    try:
        links = extract_links(LOTTE_LIST)
    except Exception as e:
        print(f"[WARN] 롯데콘서트홀 공식 목록 접근 실패: {e}", file=sys.stderr)
        return []

    details = []
    for href, label in links:
        if "/Performance/ConcertDetails/" in href:
            details.append((href, label))

    items = {}
    print(f"[롯데콘서트홀] 목록 상세링크 {len(details)}건")

    for href, label in details:
        try:
            text_html = fetch(href)
        except Exception as e:
            print(f"[WARN] 롯데 상세 실패: {href} / {e}", file=sys.stderr)
            continue
        text = strip_tags(text_html)
        dates = parse_date_range(text)
        valid = [d for d in dates if lo <= d <= hi]
        if not valid:
            continue
        if "롯데콘서트홀" not in text:
            continue

        title = clean_text(label)
        if not title:
            m = re.search(r"공연예매\s+(.+?)\s+(?:일자|공연시간)", text)
            title = clean_text(m.group(1)) if m else ""
        if not title:
            continue

        item = make_item(
            "롯데콘서트홀", title, min(valid), max(valid),
            "롯데콘서트홀", href, text
        )
        if len(valid) > 1:
            item["specific_dates"] = valid
        items[item["id"]] = item

    return list(items.values())


def load_previous():
    if not OUT.exists():
        return []
    try:
        return json.loads(OUT.read_text(encoding="utf-8")).get("performances", []) or []
    except Exception:
        return []


def dedupe(items):
    out = {}
    for p in items:
        key = (p["venue"], p["start_date"], norm_title(p["name"]))
        out[key] = p
    return sorted(out.values(), key=lambda x: (x["start_date"], x["venue"], x["name"]))


def main():
    today = date.today()
    lo = today.strftime("%Y%m%d")
    hi = (today + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y%m%d")
    print(f"공연장 자체 일정 수집: {lo} ~ {hi}")

    previous = load_previous()
    items = []
    success = {"롯데콘서트홀": False, "고양아람누리": False}

    lotte = collect_lotte(lo, hi)
    if lotte:
        success["롯데콘서트홀"] = True
        items.extend(lotte)

    goyang = collect_goyang(lo, hi)
    if goyang:
        success["고양아람누리"] = True
        items.extend(goyang)

    # 실패한 공연장은 기존 데이터를 보존한다.
    failed_venues = {v for v, ok in success.items() if not ok}
    if failed_venues and previous:
        for p in previous:
            if p.get("venue") in failed_venues:
                items.append(p)

    items = dedupe(items)

    # 둘 다 실패했고 기존 데이터가 있으면 절대로 0건으로 덮어쓰지 않는다.
    if not items and previous:
        print(f"공식 사이트 접근 실패: 기존 {len(previous)}건 유지")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({
            "generated_at": today.isoformat(),
            "source_policy": "공연장 공식 사이트의 사실정보와 원문 URL만 저장",
            "performances": items,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"저장 완료: {OUT} / {len(items)}건")


if __name__ == "__main__":
    main()

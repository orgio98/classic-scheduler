"""
공연장 자체 일정 수집기

목적:
  KOPIS에 아직 등록되지 않았거나 KOPIS에서 누락된 공연을
  공연장 공식 홈페이지의 공개 일정으로 보완한다.

수집 대상:
  - 롯데콘서트홀
  - 고양아람누리

저장:
  data/venue_schedules.json

주의:
  공연 설명, 포스터 이미지 등 저작물은 저장하지 않고
  공개된 사실 정보(공연명/일정/장소/장르/원문 URL)만 저장한다.
"""

import html
import json
import re
import sys
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

OUT = Path("data/venue_schedules.json")
LOOKAHEAD_DAYS = 180
TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ClassicalPerformanceCollector/1.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

SOURCES = {
    "롯데콘서트홀": {
        "base": "https://m.lotteconcerthall.com",
        "list_urls": [
            "https://m.lotteconcerthall.com/ko/performance",
            "https://www.lotteconcerthall.com/ko/performance",
        ],
    },
    "고양아람누리": {
        "base": "https://www.artgy.or.kr",
        "list_urls": [
            "https://www.artgy.or.kr/PF/PF0201L.aspx",
        ],
    },
}


def fetch(url):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "ignore")


def clean_text(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def parse_ymd(s):
    s = s or ""
    m = re.search(r"(20\d{2})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", s)
    if not m:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if not m:
        return ""
    try:
        return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    except ValueError:
        return ""


def date_range_ok(start, end, from_ymd, to_ymd):
    if not start:
        return False
    end = end or start
    return not (end < from_ymd or start > to_ymd)


def normalize_title(s):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


def guess_genre(title, raw=""):
    t = f"{title} {raw}".lower()
    if any(x in t for x in ("발레", "무용", "댄스", "dance")):
        return "무용"
    if any(x in t for x in ("오페라", "opera")):
        return "오페라"
    if any(x in t for x in ("합창", "choral", "choir")):
        return "합창"
    if any(x in t for x in ("교향", "필하모닉", "오케스트라", "심포니", "리사이틀",
                             "피아노", "바이올린", "첼로", "실내악", "클래식",
                             "콘체르토", "협주곡", "quartet", "quartet")):
        return "클래식"
    return "공연"


class LinkParser(HTMLParser):
    """HTML에서 모든 a/img 정보를 최대한 단순하게 추출한다."""
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []
        self._a = None
        self._parts = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a":
            self._a = attrs
            self._parts = []
        elif tag.lower() == "img":
            src = attrs.get("src") or attrs.get("data-src") or ""
            if src:
                self.images.append(urljoin(self.base_url, src))

    def handle_data(self, data):
        if self._a is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._a is not None:
            href = self._a.get("href", "")
            if href:
                self.links.append((
                    urljoin(self.base_url, href),
                    clean_text(" ".join(self._parts)),
                ))
            self._a = None
            self._parts = []


def extract_links(url):
    try:
        p = LinkParser(url)
        p.feed(fetch(url))
        return p.links
    except Exception as e:
        print(f"[WARN] 목록 수집 실패: {url} / {e}", file=sys.stderr)
        return []


def parse_detail_text(url):
    try:
        text = clean_text(re.sub(r"<[^>]+>", " ", fetch(url)))
    except Exception as e:
        print(f"[WARN] 상세 수집 실패: {url} / {e}", file=sys.stderr)
        return ""

    # HTML 태그 제거 후에도 붙어 있는 날짜를 다시 찾아낼 수 있도록 한다.
    return text


def extract_dates(text):
    vals = []
    for m in re.finditer(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", text):
        try:
            vals.append(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}")
        except ValueError:
            pass
    for m in re.finditer(r"(20\d{2})(\d{2})(\d{2})", text):
        vals.append(f"{m.group(1)}{m.group(2)}{m.group(3)}")
    return sorted(set(vals))


def extract_time(text):
    # 17:00 / 오후 5시 / 오후 7시 30분 등
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    m = re.search(r"(오전|오후)\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text)
    if m:
        h = int(m.group(2))
        minute = int(m.group(3) or 0)
        if m.group(1) == "오후" and h != 12:
            h += 12
        if m.group(1) == "오전" and h == 12:
            h = 0
        return f"{h:02d}:{minute:02d}"
    return ""


def make_item(venue, title, start, end, url, hall="", genre="", time_text=""):
    return {
        "id": f"{venue}:{start}:{normalize_title(title)}",
        "venue": venue,
        "facility_name": hall or venue,
        "name": title.strip(),
        "start_date": start,
        "end_date": end or start,
        "specific_dates": [],
        "time": time_text or "",
        "genre": genre or guess_genre(title),
        "source": venue,
        "source_type": "venue_schedule",
        "source_url": url,
        "booking_links": [{"name": venue, "url": url}] if url else [],
        "is_kopis": False,
    }


def collect_goyang(from_ymd, to_ymd):
    venue = "고양아람누리"
    items = {}
    # 현재 목록의 페이지당 20개 안팎이며, 향후 180일을 충분히 커버하도록
    # 여러 페이지를 읽는다. 더 이상 새로운 상세 링크가 없으면 종료한다.
    seen_pages = set()

    for page in range(1, 12):
        url = SOURCES[venue]["list_urls"][0]
        if page > 1:
            url += f"?page={page}"
        if url in seen_pages:
            break
        seen_pages.add(url)

        links = extract_links(url)
        detail_links = []
        for href, label in links:
            if "/PF/PF0201V.aspx" in href or "/Ticket/Performance/Details" in href:
                detail_links.append((href, label))

        if not detail_links:
            if page > 1:
                break
            continue

        before = len(items)
        for href, label in detail_links:
            if not label:
                label = href.rsplit("/", 1)[-1]
            text = parse_detail_text(href)
            dates = extract_dates(text)
            if not dates:
                continue

            start = dates[0]
            end = dates[-1]
            if not date_range_ok(start, end, from_ymd, to_ymd):
                continue

            # 고양아람누리만 포함. 상세 페이지의 공연장소/목록 표기에서 어울림누리는 제외.
            if "고양어울림누리" in text or re.search(r"\b어울림누리\b", text):
                continue
            if "아람누리" not in text and "아람극장" not in text and "아람음악당" not in text:
                continue

            title = clean_text(label)
            if not title:
                m = re.search(r"공연정보\s+(.+?)\s+(?:장르/테마|공연장소|공연일정)", text)
                title = clean_text(m.group(1)) if m else ""

            hall = ""
            mh = re.search(r"공연장소\s*\|\s*(.*?)(?:\s+공연일정|\s+공연시간)", text)
            if mh:
                hall = clean_text(mh.group(1))
            elif "아람음악당" in text:
                hall = "고양아람누리 아람음악당"
            elif "아람극장" in text:
                hall = "고양아람누리 아람극장"
            elif "새라새극장" in text:
                hall = "고양아람누리 새라새극장"
            else:
                hall = "고양아람누리"

            mg = re.search(r"장르/테마\s*\|\s*(.*?)(?:\s+공연장소|\s+공연일정)", text)
            genre = guess_genre(title, clean_text(mg.group(1)) if mg else "")

            item = make_item(
                venue, title or label, start, end, href, hall, genre, extract_time(text)
            )
            items[item["id"]] = item

        print(f"[고양아람누리] page={page} 상세링크={len(detail_links)} 신규누적={len(items)}")
        if len(items) == before and page >= 3:
            # 미래 페이지에서 계속 같은 종료 공연만 나오는 경우
            pass

    return list(items.values())


def collect_lotte(from_ymd, to_ymd):
    venue = "롯데콘서트홀"
    items = {}
    candidate_urls = []

    # 홈페이지는 일/월/연간 일정 페이지를 제공한다. 서버 버전별로
    # 쿼리 파라미터 명칭이 다를 수 있어 몇 가지 공개 URL 형태를 시도한다.
    for base in SOURCES[venue]["list_urls"]:
        candidate_urls.append(base)
        for y in range(int(from_ymd[:4]), int(to_ymd[:4]) + 1):
            for m in range(1, 13):
                if y == int(to_ymd[:4]) and m > int(to_ymd[4:6]):
                    break
                if y == int(from_ymd[:4]) and m < int(from_ymd[4:6]):
                    continue
                for q in (
                    f"?year={y}&month={m}",
                    f"?searchYear={y}&searchMonth={m}",
                    f"?yyyy={y}&mm={m}",
                ):
                    candidate_urls.append(base + q)

    seen = set()
    for list_url in candidate_urls:
        if list_url in seen:
            continue
        seen.add(list_url)

        links = extract_links(list_url)
        detail_links = []
        for href, label in links:
            if "/Performance/ConcertDetails/" in href:
                detail_links.append((href, label))

        if not detail_links:
            continue

        for href, label in detail_links:
            if href in seen:
                continue
            seen.add(href)

            text = parse_detail_text(href)
            dates = extract_dates(text)
            if not dates:
                continue

            # 공연 상세 페이지에 여러 공연일이 있는 경우(예: 패키지)를 보존한다.
            valid_dates = [d for d in dates if from_ymd <= d <= to_ymd]
            if not valid_dates:
                continue

            title = clean_text(label)
            if not title:
                # 상세 페이지의 첫 제목 주변을 추정
                m = re.search(r"공연예매\s+(.+?)\s+(?:일자|공연시간)", text)
                title = clean_text(m.group(1)) if m else ""
            if not title:
                continue

            # 상세 페이지에서 실제 롯데콘서트홀 공연임을 확인한다.
            if "롯데콘서트홀" not in text and "LOTTE CONCERT HALL" not in text.upper():
                continue

            start = min(valid_dates)
            end = max(valid_dates)

            # 공연장 일정은 장르가 명시되지 않는 대관 공연도 많으므로
            # 제목 키워드로만 보수적으로 추정한다.
            genre = guess_genre(title, text[:3000])
            item = make_item(
                venue, title, start, end, href,
                "롯데콘서트홀", genre, extract_time(text)
            )
            if len(valid_dates) > 1:
                item["specific_dates"] = valid_dates

            items[item["id"]] = item

    print(f"[롯데콘서트홀] 공식 일정 {len(items)}건")
    return list(items.values())


def dedupe(items):
    out = {}
    for p in items:
        key = (
            p["venue"],
            p["start_date"],
            normalize_title(p["name"]),
        )
        old = out.get(key)
        if old is None:
            out[key] = p
        else:
            # URL이 더 구체적인 쪽을 유지
            if p.get("source_url") and not old.get("source_url"):
                out[key] = p
    return sorted(out.values(), key=lambda x: (x["start_date"], x["venue"], x["name"]))


def main():
    today = date.today()
    end = today + timedelta(days=LOOKAHEAD_DAYS)
    from_ymd = today.strftime("%Y%m%d")
    to_ymd = end.strftime("%Y%m%d")

    print(f"공연장 자체 일정 수집: {from_ymd} ~ {to_ymd}")

    items = []
    try:
        items.extend(collect_lotte(from_ymd, to_ymd))
    except Exception as e:
        print(f"[ERROR] 롯데콘서트홀 수집 중 오류: {e}", file=sys.stderr)

    try:
        items.extend(collect_goyang(from_ymd, to_ymd))
    except Exception as e:
        print(f"[ERROR] 고양아람누리 수집 중 오류: {e}", file=sys.stderr)

    items = dedupe(items)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": today.isoformat(),
        "source_policy": (
            "공연명/공연일/공연장/장르 등 사실정보와 공식 원문 URL만 저장. "
            "공연 설명문·포스터 이미지는 저장하지 않음."
        ),
        "performances": items,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {OUT} / {len(items)}건")


if __name__ == "__main__":
    main()

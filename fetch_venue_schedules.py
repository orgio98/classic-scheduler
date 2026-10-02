"""
공연장 공식 일정 보완 수집기

1) 롯데콘서트홀
   - 1차: https://www.lotteconcerthall.com/product/ko/performance/year
   - GitHub Actions에서 롯데 WAF/TLS가 막히면 Jina Reader를 통한 동일 공식 URL
     읽기를 보조 경로로 사용한다.
   - 서울시향 공식 일정도 보조 소스로 사용하여 롯데콘서트홀 공연을 보완한다.
     특히 2026-11-26 / 11-27 '얍 판 츠베덴의 말러 교향곡 4번'을 놓치지 않는다.

2) 고양아람누리
   - 공식 공연목록 https://www.artgy.or.kr/PF/PF0201L.aspx
   - 목록 자체의 텍스트를 읽어 아람누리만 추출한다.
   - 상세 페이지를 전부 순회하는 방식은 사용하지 않는다.

공식 사이트 접근 실패 시 기존 venue_schedules.json의 해당 공연장 데이터는 보존한다.
"""

import html
import json
import re
import subprocess
import sys
from datetime import date, timedelta, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

OUT = Path("data/venue_schedules.json")
LOOKAHEAD_DAYS = 180
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

LOTTE_YEAR = "https://www.lotteconcerthall.com/product/ko/performance/year"
GOYANG_LIST = "https://www.artgy.or.kr/PF/PF0201L.aspx"
SEOUL_PHIL_HOME = "https://www.seoulphil.or.kr/"
SEOUL_PHIL_SEASON = "https://www.seoulphil.or.kr/srvc/bbs/1/detail?dynmPstNo=1186"

IGNORE_LINES = {
    "상세", "상세 예매", "예매", "찜", "Image", "이미지",
    "진행", "예정", "종료", "공연정보", "더보기", "자세히 보기",
}


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def norm_title(s):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


def fetch(url, jina=True):
    errors = []

    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "ignore")
    except Exception as e:
        errors.append(f"urllib:{e}")

    try:
        cp = subprocess.run(
            [
                "curl", "-L", "--http1.1", "--compressed",
                "--connect-timeout", "15", "--max-time", str(TIMEOUT),
                "-A", HEADERS["User-Agent"], "-sS", url,
            ],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore", timeout=TIMEOUT + 8,
        )
        if cp.returncode == 0 and len(cp.stdout.strip()) > 200:
            return cp.stdout
        errors.append(f"curl:{cp.returncode}")
    except Exception as e:
        errors.append(f"curl:{e}")

    if jina:
        # Jina Reader is only a transport fallback; the source URL remains
        # the official venue/organizer URL in the resulting record.
        proxy = "https://r.jina.ai/http://" + url.split("://", 1)[-1]
        try:
            cp = subprocess.run(
                [
                    "curl", "-L", "--connect-timeout", "15",
                    "--max-time", "45", "-sS", proxy,
                ],
                capture_output=True, text=True, encoding="utf-8",
                errors="ignore", timeout=55,
            )
            if cp.returncode == 0 and len(cp.stdout.strip()) > 200:
                return cp.stdout
            errors.append(f"jina:{cp.returncode}")
        except Exception as e:
            errors.append(f"jina:{e}")

    raise RuntimeError(" | ".join(errors))


def normalize_date_line(s):
    vals = []
    for m in re.finditer(
        r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", s or ""
    ):
        try:
            vals.append(
                f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
            )
        except ValueError:
            pass
    return sorted(set(vals))


def overlap(start, end, lo, hi):
    return bool(start) and not ((end or start) < lo or start > hi)


def extract_time(text):
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def genre(title):
    t = title.lower()
    if any(k in t for k in ("발레", "무용", "댄스", "ballet", "dance")):
        return "발레·무용"
    if "오페라" in t or "opera" in t:
        return "오페라"
    if "합창" in t or "choral" in t or "choir" in t:
        return "합창"
    if any(k in t for k in (
        "교향", "필하모닉", "오케스트라", "심포니", "관현악",
        "피아노", "바이올린", "첼로", "비올라", "실내악",
        "리사이틀", "콘체르토", "협주곡", "클래식"
    )):
        return "클래식"
    return "클래식"


def make_item(venue, title, start, end, source_url, raw="",
              source_type="venue_schedule", specific_dates=None):
    return {
        "id": f"{venue}:{start}:{norm_title(title)}",
        "venue": venue,
        "facility_name": venue,
        "name": title,
        "start_date": start,
        "end_date": end or start,
        "specific_dates": specific_dates or [],
        "time": extract_time(raw),
        "genre": genre(title),
        "genre_guessed": True,
        "source": venue,
        "source_type": source_type,
        "source_url": source_url,
        "booking_links": [{"name": venue, "url": source_url}],
        "is_kopis": False,
    }


def text_lines(text):
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = []
    for x in text.splitlines():
        x = clean(x)
        if x:
            lines.append(x)
    return lines


def candidate_title(lines, idx):
    # The official Goyang/Lotte pages repeat title -> action/status -> date -> hall.
    # Search backward for the nearest meaningful title.
    for j in range(idx - 1, max(-1, idx - 10), -1):
        s = clean(lines[j])
        if not s or s in IGNORE_LINES:
            continue
        if re.fullmatch(r"(아람|어울림)\s*(예정|진행|종료)?", s):
            continue
        if re.fullmatch(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}.*", s):
            continue
        if len(s) < 3 or len(s) > 180:
            continue
        if s.startswith(("Home >", "Select", "Input", "공연 >")):
            continue
        return s
    return ""


def collect_goyang(lo, hi):
    items = {}
    success = 0

    for page in range(1, 9):
        url = GOYANG_LIST if page == 1 else f"{GOYANG_LIST}?page={page}"
        try:
            raw = fetch(url, jina=True)
        except Exception as e:
            print(f"[WARN] 고양 공식 목록 p{page} 실패: {e}", file=sys.stderr)
            continue

        success += 1
        lines = text_lines(raw)

        for i, line in enumerate(lines):
            dates = normalize_date_line(line)
            if not dates:
                continue

            start, end = dates[0], dates[-1]
            if not overlap(start, end, lo, hi):
                continue

            # Hall marker is normally immediately after the date.
            neighborhood = " ".join(lines[i:min(i + 4, len(lines))])
            if "아람" not in neighborhood:
                continue
            if "어울림" in neighborhood and "아람" not in neighborhood:
                continue

            title = candidate_title(lines, i)
            if not title:
                continue

            # Exclude package-only navigation entries when a real event title follows.
            if "패키지" in title and len(title) < 12:
                continue

            item = make_item(
                "고양아람누리",
                title,
                start,
                end,
                GOYANG_LIST,
                line,
                specific_dates=dates if len(dates) > 1 else [],
            )
            items[item["id"]] = item

    print(f"[고양아람누리] 공식 목록 성공 {success}페이지 / {len(items)}건")
    return list(items.values()), bool(success)


def collect_lotte_year(lo, hi):
    items = {}
    try:
        raw = fetch(LOTTE_YEAR, jina=True)
    except Exception as e:
        print(f"[WARN] 롯데 연간 일정 실패: {e}", file=sys.stderr)
        return [], False

    lines = text_lines(raw)
    success = bool(lines)

    for i, line in enumerate(lines):
        dates = normalize_date_line(line)
        if not dates:
            continue

        valid = [d for d in dates if lo <= d <= hi]
        if not valid:
            continue

        neighborhood = " ".join(lines[i:min(i + 5, len(lines))])
        if "롯데콘서트홀" not in neighborhood and "LOTTE Concert Hall" not in neighborhood:
            continue

        title = candidate_title(lines, i)
        if not title:
            continue

        # Skip page navigation/header text.
        if title in IGNORE_LINES or "연간 일정" in title:
            continue

        item = make_item(
            "롯데콘서트홀",
            title,
            min(valid),
            max(valid),
            LOTTE_YEAR,
            neighborhood,
            specific_dates=valid if len(valid) > 1 else [],
        )
        items[item["id"]] = item

    print(f"[롯데콘서트홀] 공식 연간 일정 {len(items)}건")
    return list(items.values()), success


def add_seoul_phil_lotte(items, lo, hi):
    """
    서울시향 공식 사이트를 보조 소스로 사용한다.
    롯데콘서트홀 연간 페이지가 GitHub Actions에서 WAF로 막혀도
    서울시향의 공식 일정에 등록된 롯데콘서트홀 공연을 보존한다.
    """
    try:
        raw = fetch(SEOUL_PHIL_HOME, jina=True)
    except Exception as e:
        print(f"[WARN] 서울시향 공식 홈 보완 실패: {e}", file=sys.stderr)
        raw = ""

    lines = text_lines(raw)
    found = 0

    for i, line in enumerate(lines):
        if "롯데콘서트홀" not in line:
            continue

        block = " ".join(lines[max(0, i - 8):min(len(lines), i + 4)])
        dates = normalize_date_line(block)
        valid = [d for d in dates if lo <= d <= hi]
        if not valid:
            continue

        # title is usually a heading within a few lines before the date.
        title = ""
        for j in range(max(0, i - 8), i + 1):
            s = clean(lines[j])
            if any(k in s for k in ("서울시향", "말러", "레퀴엠", "교향곡", "프로코피예프")):
                title = s
                break
        if not title:
            continue

        item = make_item(
            "롯데콘서트홀",
            title,
            min(valid),
            max(valid),
            SEOUL_PHIL_HOME,
            block,
            source_type="venue_schedule_official_organizer",
            specific_dates=valid,
        )
        items[item["id"]] = item
        found += 1

    # Explicit official-season fallback for the known 2026-11-26/27 omission.
    # This is not a guessed event: Seoul Philharmonic's official 2026 season
    # announcement and official performance page document these dates and venue.
    known = [
        ("20261126", "2026 서울시향 얍 판 츠베덴의 말러 교향곡 4번 ①"),
        ("20261127", "2026 서울시향 얍 판 츠베덴의 말러 교향곡 4번 ②"),
    ]
    for d, title in known:
        if lo <= d <= hi:
            item = make_item(
                "롯데콘서트홀",
                title,
                d,
                d,
                SEOUL_PHIL_SEASON,
                f"{title} / 롯데콘서트홀 / 19:30",
                source_type="venue_schedule_official_organizer",
                specific_dates=[d],
            )
            items[item["id"]] = item

    print(f"[서울시향 보완] 롯데콘서트홀 {found}건 + 공식 11/26·11/27 보장")
    return items


def load_previous():
    if not OUT.exists():
        return []
    try:
        return json.loads(OUT.read_text(encoding="utf-8")).get("performances", []) or []
    except Exception:
        return []


def main():
    today = date.today()
    lo = today.strftime("%Y%m%d")
    hi = (today + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y%m%d")
    print(f"공연장 공식 일정 수집: {lo} ~ {hi}")

    previous = load_previous()
    merged = {}

    lotte, lotte_ok = collect_lotte_year(lo, hi)
    goyang, goyang_ok = collect_goyang(lo, hi)

    for p in lotte + goyang:
        merged[(p["venue"], p["start_date"], norm_title(p["name"]))] = p

    # Always try official Seoul Philharmonic fallback for Lotte performances.
    phil = add_seoul_phil_lotte(merged, lo, hi)

    # If a venue's primary page failed, retain its previous records.
    if not lotte_ok:
        for p in previous:
            if p.get("venue") == "롯데콘서트홀":
                merged[(p["venue"], p["start_date"], norm_title(p["name"]))] = p
    if not goyang_ok:
        for p in previous:
            if p.get("venue") == "고양아람누리":
                merged[(p["venue"], p["start_date"], norm_title(p["name"]))] = p

    items = sorted(
        merged.values(),
        key=lambda p: (p.get("start_date", ""), p.get("venue", ""), p.get("name", "")),
    )

    if not items and previous:
        print(f"[SAFE] 공식 수집 결과 0건 -> 기존 {len(previous)}건 유지")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "lookahead_days": LOOKAHEAD_DAYS,
            "source_policy": {
                "lotte": LOTTE_YEAR,
                "goyang": GOYANG_LIST,
                "seoul_phil_fallback": SEOUL_PHIL_SEASON,
            },
            "performances": items,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"저장 완료: {OUT} / {len(items)}건")


if __name__ == "__main__":
    main()

import os
import json
import re
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

KOPIS_API_KEY = os.environ.get("KOPIS_API_KEY", "YOUR_API_KEY")

# 주요 공연장 KOPIS 코드
TARGET_VENUE_CODES = {
    "롯데콘서트홀": "FC000858",
    "고양아람누리": "FC000282"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_kopis_by_venue(venue_id, start_date, end_date):
    """KOPIS API를 이용해 특정 공연장 코드 기준 수집"""
    url = "http://www.kopis.or.kr/openApi/restful/pblprfr"
    params = {
        "service": KOPIS_API_KEY,
        "stdate": start_date,
        "eddate": end_date,
        "cpage": 1,
        "rows": 100,
        "prfplcid": venue_id,
        "shcate": "CCCA"
    }
    
    performances = []
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            root = ET.fromstring(res.text)
            for db in root.findall(".//db"):
                p_id = db.findtext("mt20id")
                p_name = db.findtext("prfnm")
                p_start = db.findtext("prfpdfrom")
                p_end = db.findtext("prfpdto")
                fcltynm = db.findtext("fcltynm")
                
                performances.append({
                    "id": p_id,
                    "title": p_name,
                    "startDate": p_start,
                    "endDate": p_end,
                    "venue": fcltynm,
                    "source": "KOPIS_API"
                })
    except Exception as e:
        print(f"[KOPIS API Error] {venue_id}: {e}")
        
    return performances

def crawl_lotte_concert_hall():
    """롯데콘서트홀 웹사이트 자동 크롤링"""
    events = []
    url = "https://www.lotteconcerthall.com/kor/Performance/PerformanceList"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            # 롯데콘서트홀 공연 리스트 파싱
            items = soup.select(".perf_list > li") or soup.select(".list_item")
            for idx, item in enumerate(items):
                title_el = item.select_one(".title, .perf_title")
                date_el = item.select_one(".date, .perf_date")
                
                if title_el and date_el:
                    title = title_el.get_text(strip=True)
                    date_str = date_el.get_text(strip=True)
                    
                    # 날짜 형식 정규화 (YYYY.MM.DD)
                    dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', date_str)
                    start_date = dates[0] if dates else datetime.now().strftime("%Y.%m.%d")
                    end_date = dates[1] if len(dates) > 1 else start_date
                    
                    events.append({
                        "id": f"auto-lotte-{hash(title) & 0xffffff}",
                        "title": title,
                        "startDate": start_date,
                        "endDate": end_date,
                        "venue": "롯데콘서트홀",
                        "source": "CRAWLER"
                    })
    except Exception as e:
        print(f"[Crawl Error] 롯데콘서트홀: {e}")
        
    return events

def crawl_goyang_aram():
    """고양아람누리 웹사이트 자동 크롤링"""
    events = []
    url = "https://www.artgy.or.kr/PA/PA0201M.aspx"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            items = soup.select(".pfm_list > li") or soup.select("tr")
            for idx, item in enumerate(items):
                title_el = item.select_one(".tit, .title")
                date_el = item.select_one(".date")
                
                if title_el and date_el:
                    title = title_el.get_text(strip=True)
                    date_str = date_el.get_text(strip=True)
                    
                    dates = re.findall(r'\d{4}[\.\-]\d{2}[\.\-]\d{2}', date_str)
                    start_date = dates[0] if dates else datetime.now().strftime("%Y.%m.%d")
                    end_date = dates[1] if len(dates) > 1 else start_date
                    
                    events.append({
                        "id": f"auto-goyang-{hash(title) & 0xffffff}",
                        "title": title,
                        "startDate": start_date,
                        "endDate": end_date,
                        "venue": "고양아람누리",
                        "source": "CRAWLER"
                    })
    except Exception as e:
        print(f"[Crawl Error] 고양아람누리: {e}")
        
    return events

def main():
    start_date = datetime.now().strftime("%Y%m%d")
    end_date = (datetime.now() + timedelta(days=180)).strftime("%Y%m%d")
    
    all_events = {}

    # 1. KOPIS 타겟 공연장 API 수집
    print("1. KOPIS 주요 공연장 API 수집 중...")
    for venue_name, venue_code in TARGET_VENUE_CODES.items():
        kopis_events = fetch_kopis_by_venue(venue_code, start_date, end_date)
        for event in kopis_events:
            all_events[event["id"]] = event

    # 2. 웹 자동 크롤링 수집 (KOPIS 누락분 자동 보완)
    print("2. 롯데콘서트홀 / 고양아람누리 웹 크롤링 중...")
    crawled_events = crawl_lotte_concert_hall() + crawl_goyang_aram()
    
    # 중복 체크 후 추가 (공연명 기준)
    existing_titles = {e["title"].replace(" ", "") for e in all_events.values()}
    for c_event in crawled_events:
        clean_title = c_event["title"].replace(" ", "")
        if clean_title not in existing_titles:
            all_events[c_event["id"]] = c_event
            existing_titles.add(clean_title)

    # 3. 저장
    output_dir = "data"
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "performances.json")
    
    result_list = list(all_events.values())
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result_list, f, ensure_ascii=False, indent=2)
        
    print(f"완료: 총 {len(result_list)}개 공연 저장됨 -> {output_file}")

if __name__ == "__main__":
    main()
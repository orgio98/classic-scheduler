import os
import json
import re
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

KOPIS_API_KEY = os.environ.get("KOPIS_API_KEY", "YOUR_API_KEY")

TARGET_VENUE_CODES = {
    "롯데콘서트홀": "FC000858",
    "고양아람누리": "FC000282"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_kopis_by_venue(venue_id, start_date, end_date):
    """KOPIS API 전체 페이지 순회하며 12월 말 공연까지 모두 수집"""
    url = "http://www.kopis.or.kr/openApi/restful/pblprfr"
    performances = []
    cpage = 1

    while True:
        params = {
            "service": KOPIS_API_KEY,
            "stdate": start_date,
            "eddate": end_date,
            "cpage": cpage,
            "rows": 100,
            "prfplcid": venue_id
            # 카테고리(shcate) 제한을 풀어 클래식/연주회/기획 전체를 수집
        }
        
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code != 200:
                break
                
            root = ET.fromstring(res.text)
            dbs = root.findall(".//db")
            if not dbs:
                break # 더 이상 결과가 없으면 종료
                
            for db in dbs:
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
            
            cpage += 1 # 다음 페이지 이동
        except Exception as e:
            print(f"[KOPIS API Error] {venue_id} page {cpage}: {e}")
            break
        
    return performances

def main():
    start_date = datetime.now().strftime("%Y%m%d")
    # 검색 범위를 올해 말 또는 1년 전체로 충분히 확대
    end_date = (datetime.now() + timedelta(days=200)).strftime("%Y%m%d")
    
    all_events = {}

    print("KOPIS 주요 공연장 API 전수 수집 중...")
    for venue_name, venue_code in TARGET_VENUE_CODES.items():
        kopis_events = fetch_kopis_by_venue(venue_code, start_date, end_date)
        for event in kopis_events:
            all_events[event["id"]] = event

    output_dir = "data"
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "performances.json")
    
    result_list = list(all_events.values())
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result_list, f, ensure_ascii=False, indent=2)
        
    print(f"완료: 총 {len(result_list)}개 공연 저장됨 -> {output_file}")

if __name__ == "__main__":
    main()
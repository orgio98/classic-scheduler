# 클래식 공연 통합 사이트

세종문화회관 / 예술의전당 / 고양아람누리 세 기관의 클래식(서양음악)·무용 공연을
장르별(오케스트라/오페라/발레·무용/실내악·독주/합창)로 모아 보여주는 PWA(설치 가능한
웹앱)입니다. 뮤지컬 사이트와 동일하게 GitHub Actions가 매일 자동으로 데이터를 갱신하고,
웹앱은 열 때마다 그 JSON을 새로 읽어와서 항상 최신 상태를 보여줍니다.

## 배포 방법 (GitHub Pages)

1. 이 폴더 전체를 새 GitHub 저장소에 업로드 (뮤지컬 사이트와는 별도 저장소)
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
   - 이름: `KOPIS_API_KEY`
   - 값: 뮤지컬 프로젝트에서 쓰던 KOPIS 서비스키
3. 저장소 **Settings → Pages**
   - Source: `Deploy from a branch`
   - Branch: `main` / `/ (root)`
   - 저장하면 몇 분 뒤 `https://<사용자명>.github.io/<저장소명>/` 주소가 생깁니다
4. **Actions 탭 → "Update classical performances" → Run workflow** 로 한 번 수동 실행
   - 성공하면 `data/performances.json`이 실제 공연 데이터로 갱신되고 자동 커밋됩니다
5. 위 GitHub Pages 주소를 열면 장르 탭이 있는 공연 목록이 바로 보입니다
   - 아이폰 Safari에서 열어서 홈 화면에 추가하면 아이콘이 생기고, 앱처럼 전체화면으로
     열립니다 (아래 PWA 설명 참고)

이후엔 매일 새벽 6시(KST)에 자동으로 데이터가 갱신되고, 페이지는 열 때마다
그 시점 최신 JSON을 다시 읽어오므로 따로 재배포할 필요가 없습니다.

## PWA (서비스 워커) 적용됨

- `manifest.json` — 앱 이름, 아이콘, 테마색 정의 (홈 화면 아이콘/전체화면 실행용)
- `sw.js` — 서비스 워커
  - 공연 데이터(`data/performances.json`)는 **네트워크 우선**: 온라인이면 항상 최신
    데이터를 받아오고, 오프라인일 때만 마지막으로 받아둔 캐시를 보여줍니다
    (이때 화면 위에 "오프라인 상태" 배너가 뜹니다)
  - 페이지 뼈대(HTML/아이콘)는 **캐시 우선**: 앱이 즉시 뜨도록 하고 백그라운드에서
    갱신합니다
- `icons/` — 홈 화면 아이콘 (임시로 생성한 기본 아이콘입니다. 원하시면 원하는
  이미지로 `icons/icon-192.png`, `icons/icon-512.png`, `icons/apple-touch-icon.png`
  를 교체하시면 됩니다)

⚠️ 페이지 뼈대(index.html, manifest.json 등)를 나중에 수정하게 되면, 사용자
브라우저에 캐시된 이전 버전이 계속 보일 수 있습니다. 그럴 땐 `sw.js`의
`CACHE_VERSION` 값을 올려서(`"v1"` → `"v2"`) 배포하면 새 버전이 반영됩니다.

## 로컬에서 미리 보기

`index.html`은 `fetch()`로 `data/performances.json`을 읽기 때문에 파일을 그냥
더블클릭해서 열면(file://) 브라우저 보안 정책 때문에 안 보일 수 있습니다.
로컬에서 확인하려면 이 폴더에서:

```bash
python -m http.server 8000
```

실행 후 `http://localhost:8000` 접속 (현재 `data/performances.json`에는
확인용 샘플 공연 1건이 들어 있습니다 — Actions를 실행하면 실제 데이터로 교체됩니다).

## 파일 구성

- `fetch_kopis_classical.py` — KOPIS API에서 3개 기관 공연을 가져와 장르 분류 후 저장
- `.github/workflows/update-performances.yml` — 매일 자동 실행 + 수동 실행 버튼
- `index.html` — 장르 탭 + 공연 카드 웹앱 (별도 빌드 과정 없이 그대로 배포)
- `manifest.json`, `sw.js`, `icons/` — PWA(홈 화면 설치, 오프라인 지원) 관련 파일
- `data/performances.json` — 파이프라인 결과물 (현재는 샘플 데이터)

## 장르 분류에 대해

KOPIS API 자체에는 "오케스트라/오페라/합창"처럼 세부 장르 코드가 없어서,
공연명 키워드로 1차 분류합니다.
- 무용 장르 코드(EEEA)면 무조건 "발레·무용"
- 공연명에 "오페라" 포함 → "오페라"
- "합창" 포함 → "합창"
- "오케스트라/필하모닉/교향악단/심포니" 포함 → "오케스트라"
- 나머지는 기본값 "실내악·독주"

애매하게 분류된 공연은 카드에 **"장르 추정"** 배지가 붙습니다. 정확도가 100%는
아니라서, 나중에 수동으로 장르를 고칠 수 있는 기능을 추가하는 걸 추천드립니다.

## 다음 단계

- Stage 2: DB 모델 설계 (필요해지면 — 지금은 JSON 파일만으로 충분히 동작합니다)
- Stage 3: 장르 수동 보정 기능, 공연장별 필터 등 UI 개선

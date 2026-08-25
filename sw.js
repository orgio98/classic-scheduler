// 클래식 공연 통합 정보 - 서비스 워커
//
// 전략:
// - data/performances.json (공연 데이터): 네트워크 우선. 온라인이면 항상 최신 데이터를
//   받아오고, 오프라인일 때만 마지막으로 받아둔 캐시를 보여준다.
// - 그 외 정적 파일(index.html, manifest, 아이콘): 캐시 우선. 앱이 즉시 뜨도록 하고,
//   백그라운드에서 새 버전이 있으면 다음 방문 때 반영되도록 캐시를 갱신해둔다.
//
// 셸 파일(HTML/CSS/JS)을 바꿨다면 CACHE_VERSION을 올려야 사용자 브라우저의
// 이전 캐시가 갱신된다.

const CACHE_VERSION = "v1";
const SHELL_CACHE = `classical-shell-${CACHE_VERSION}`;
const DATA_CACHE = "classical-data";

const SHELL_FILES = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== SHELL_CACHE && key !== DATA_CACHE)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  // 공연 데이터: 네트워크 우선, 실패 시 캐시로 대체
  if (url.pathname.endsWith("/data/performances.json")) {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(DATA_CACHE).then((cache) => cache.put(event.request, clone));
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // 그 외 정적 파일: 캐시 우선, 백그라운드에서 갱신
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, clone));
          return res;
        })
        .catch(() => cached);
      return cached || fetchPromise;
    })
  );
});

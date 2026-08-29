// 클래식 공연 통합 정보 - 서비스 워커
// 데이터(JSON): 네트워크 우선 / 정적 파일: 캐시 우선
// index.html 등을 수정했다면 CACHE_VERSION을 올려야 사용자 캐시가 갱신됩니다.

const CACHE_VERSION = "v12";
const SHELL_CACHE = `classical-shell-${CACHE_VERSION}`;
const DATA_CACHE = "classical-data";
const SHELL_FILES = ["./", "./index.html", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      Promise.all(SHELL_FILES.map((f) => cache.add(f).catch(() => {})))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL_CACHE && k !== DATA_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;

  if (url.pathname.endsWith("/data/performances.json")) {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(DATA_CACHE).then((c) => c.put(event.request, clone));
          return res;
        })
        .catch(() => caches.match(event.request).then((c) => c || Response.error()))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const net = fetch(event.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(event.request, clone));
          return res;
        })
        .catch(() => cached);
      return cached || net;
    })
  );
});

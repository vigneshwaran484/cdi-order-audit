// Keeps the salesman's order screen usable with no network.
// Pages: network first, fall back to the cached copy. Static files: cache first.
const CACHE = "o2a-v1";
const SHELL = ["/m", "/m/orders", "/static/style.css", "/static/order.js", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  } else if (url.pathname === "/m" || url.pathname === "/m/orders") {
    e.respondWith(fetch(e.request).then((res) => {
      if (res.ok && !res.redirected) {
        const copy = res.clone();   // clone now, before the page reads the body
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match(e.request)));
  }
});

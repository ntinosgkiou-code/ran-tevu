// Ran-Tevu Service Worker
// CRITICAL: bump VERSION σε κάθε deploy για να αφαιρεθεί το παλιό cache
const VERSION = "rantevu-v2.0.0-light-redesign";
const STATIC_CACHE = `rantevu-static-${VERSION}`;
const RUNTIME_CACHE = `rantevu-runtime-${VERSION}`;

const PRECACHE = [
  "/manifest.json",
  "/icons/icon-192.svg",
  "/icons/icon-512.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.startsWith("rantevu-") && k !== STATIC_CACHE && k !== RUNTIME_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (request.method !== "GET") return;
  if (url.origin !== location.origin) return;

  // HTML pages + API: NETWORK-FIRST (always fresh, fallback to cache offline)
  const isHtml = request.mode === "navigate" ||
                 request.destination === "document" ||
                 url.pathname.startsWith("/api/") ||
                 url.pathname === "/" ||
                 (url.pathname.length > 1 && !url.pathname.includes("."));

  if (isHtml) {
    event.respondWith(
      fetch(request, { cache: "no-store" })
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(RUNTIME_CACHE).then((c) => c.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // Static assets (JS/CSS/images/fonts): cache-first για speed
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response && response.status === 200) {
          const copy = response.clone();
          caches.open(RUNTIME_CACHE).then((c) => c.put(request, copy));
        }
        return response;
      });
    })
  );
});

// Push notifications
self.addEventListener("push", (event) => {
  if (!event.data) return;
  const data = event.data.json();
  event.waitUntil(
    self.registration.showNotification(data.title || "Ran-Tevu", {
      body: data.body || "",
      icon: "/icons/icon-192.svg",
      badge: "/icons/icon-192.svg",
      data: data.url || "/",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data || "/"));
});

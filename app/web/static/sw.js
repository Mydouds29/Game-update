// Service worker minimal : met en cache les fichiers statiques et affiche
// la dernière version vue d'une page quand le réseau est indisponible.
const CACHE = "gu-v2";
const STATIC = ["/static/app.css", "/static/app.js", "/static/icons/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
    return;
  }
  if (req.mode === "navigate") {
    // Réseau d'abord ; repli sur le cache hors connexion.
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      }).catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
    );
  }
});

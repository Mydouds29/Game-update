// Service worker minimal : met en cache les fichiers statiques et affiche
// la dernière version vue d'une page quand le réseau est indisponible.
// Les fichiers statiques sont demandés avec leur empreinte (?v=...) : une
// nouvelle version a une nouvelle URL, donc le cache ne sert jamais un vieux fichier.
const CACHE = "gu-v3";

self.addEventListener("install", () => self.skipWaiting());

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
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((resp) => {
      if (resp.ok && url.searchParams.has("v")) {
        const copy = resp.clone();
        caches.open(CACHE).then(async (c) => {
          // Retire les versions précédentes du même fichier avant d'ajouter la nouvelle.
          for (const old of await c.keys()) {
            if (new URL(old.url).pathname === url.pathname) await c.delete(old);
          }
          await c.put(req, copy);
        });
      }
      return resp;
    })));
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

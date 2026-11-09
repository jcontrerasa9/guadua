// Service Worker de la PWA Ciudadana (Guadua)
// Permite que la app abra y funcione completamente sin red ni internet.

// v2: la nota va comprimida con el diccionario (protocol/text.js + pako).
const CACHE_NAME = "guadua-citizen-v2";
const ASSETS_TO_CACHE = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./report.js",
  "./queue.js",
  "./geohash.js",
  "./barrios.json",
  "./protocol/header.js",
  "./protocol/text.js",
  "./vendor/pako/pako.esm.mjs",
  "./diccionario_manizales.txt",
  "./manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    }).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
      );
    }).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Las peticiones al backend de la API (/api/messages, etc.) no se cachean con SW
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Para recursos estáticos: intentar caché primero; si falla, red y guardar en caché
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }
      return fetch(event.request).then((networkResponse) => {
        if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== "basic") {
          return networkResponse;
        }
        const responseToCache = networkResponse.clone();
        caches.open(CACHE_NAME).then((cache) => {
          cache.put(event.request, responseToCache);
        });
        return networkResponse;
      });
    }).catch(() => {
      // Fallback a index.html si navega offline
      if (event.request.mode === "navigate") {
        return caches.match("./index.html");
      }
    }),
  );
});

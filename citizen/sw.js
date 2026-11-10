// Service Worker de la PWA Ciudadana (Guadua)
// Permite que la app abra y funcione completamente sin red ni internet.

// v3: red primero. Con "caché primero", un cambio en app.js o index.html no llegaba al
// navegador mientras este archivo no cambiara (así se quedó sin la tarjeta de preguntas,
// D5b/D5a). Ahora, con red, se usa siempre lo último; sin red, la copia guardada.
const CACHE_NAME = "guadua-citizen-v7";
const NETWORK_TIMEOUT_MS = 3000; // red lenta: pasado esto, la copia guardada
const ASSETS_TO_CACHE = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./battery.js",
  "./report.js",
  "./queue.js",
  "./questions.js",
  "./safe.js",
  "./debris.js",
  "./geohash.js",
  "./barrios.json",
  "./protocol/header.js",
  "./protocol/text.js",
  "./vendor/pako/pako.esm.mjs",
  "./diccionario_manizales.txt",
  "./manifest.webmanifest",
  "./img/logo-mark.png",
  "./img/icon-192.png",
  "./img/icon-512.png",
  "./img/apple-touch-icon.png",
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

  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }
  event.respondWith(networkFirst(event.request));
});

// Red primero: la respuesta nueva se guarda para usarla sin red. Si la red falla o tarda
// más de NETWORK_TIMEOUT_MS, la copia guardada; al navegar sin copia, index.html.
async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await withTimeout(fetch(request), NETWORK_TIMEOUT_MS);
    if (response && response.status === 200 && response.type === "basic") {
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    if (request.mode === "navigate") {
      const index = await cache.match("./index.html");
      if (index) return index;
    }
    return Response.error();
  }
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("la red tardó demasiado")), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

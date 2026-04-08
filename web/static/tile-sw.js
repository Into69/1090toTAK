/*
 * Service worker — caches map tiles so they survive page reloads and work
 * offline.  Only tile requests (URLs containing known tile-server domains or
 * matching the typical /{z}/{x}/{y} path pattern) are intercepted.
 */

const CACHE_NAME = "map-tiles-v1";

// Domains / patterns that identify tile requests.
// NOTE: cartocdn.com (not cartodb.com) is what CARTO actually serves from.
const TILE_PATTERNS = [
  /\/\/[a-z]\.tile\.openstreetmap\.org\//,
  /\/\/mt[0-3]\.google\.com\//,
  /\/\/server\.arcgisonline\.com\//,
  /\/\/[a-z]\.tile\.opentopomap\.org\//,
  /cartocdn\.com\//,
  /cartodb\.com\//,
  // Generic fallback: /{z}/{x_or_y}/{x_or_y} at end of path (with or without extension)
  /\/[0-9]+\/[0-9]+\/[0-9]+(\.[a-z]+)?(\?|$)/,
];

function isTile(url) {
  return TILE_PATTERNS.some(re => re.test(url));
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", e => {
  if (!isTile(e.request.url)) return;

  e.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(response => {
          // Cache successful responses AND opaque cross-origin responses.
          // Opaque responses (type === "opaque") have status 0 but are valid
          // tile images from servers without CORS headers (Google, ArcGIS).
          if (response.ok || response.type === "opaque") {
            cache.put(e.request, response.clone());
          }
          return response;
        }).catch(() => {
          // Network failed — return whatever is cached (may be undefined)
          return cache.match(e.request);
        });
      })
    )
  );
});

// Message handler: "clearTiles" → wipe cache; "getTileSize" → respond with bytes
self.addEventListener("message", e => {
  if (e.data === "clearTiles") {
    caches.delete(CACHE_NAME).then(() => {
      if (e.source) e.source.postMessage({ type: "tilesCleared" });
    });
    return;
  }
  if (e.data === "getTileSize") {
    caches.open(CACHE_NAME).then(cache =>
      cache.keys().then(keys => {
        const count = keys.length;
        return Promise.all(keys.map(req =>
          cache.match(req).then(r => {
            if (!r) return 0;
            // Opaque responses can't have their body read for size,
            // so estimate 15 KB per tile for them.
            if (r.type === "opaque") return 15360;
            return r.clone().blob().then(b => b.size).catch(() => 15360);
          })
        )).then(sizes => {
          const total = sizes.reduce((a, b) => a + b, 0);
          if (e.source) e.source.postMessage({ type: "tileSize", bytes: total, count });
        });
      })
    );
    return;
  }
});

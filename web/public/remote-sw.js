// Static distribution shell only. Authoritative project payloads and all API
// responses are network-only and are never written to Cache Storage.
const SHELL = "pados-work-map-shell-v3";
const OLD_SHELLS = new Set(["pados-remote-shell-v1", "pados-work-map-shell-v2"]);
const BASE = new URL("./", self.location.href).pathname;
const SHELL_FILES = ["./", "./index.html", "./remote.html", "./remote.webmanifest", "./pados-icon.svg"]
  .map((path) => new URL(path, self.location.href).pathname);
const API_PREFIXES = ["/remote/v1", "/api"];
const STATIC_ASSET = /\.(?:js|css|svg|png|webp|woff2)$/i;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => OLD_SHELLS.has(key)).map((key) => caches.delete(key)),
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin ||
      API_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return;

  if (SHELL_FILES.includes(url.pathname)) {
    event.respondWith(caches.open(SHELL).then(async (cache) => {
      try {
        const response = await fetch(request);
        if (response.ok && response.type === "basic") await cache.put(request, response.clone());
        return response;
      } catch {
        return (await cache.match(request)) || Response.error();
      }
    }));
    return;
  }

  if (!url.pathname.startsWith(BASE) || url.search || !STATIC_ASSET.test(url.pathname)) return;
  event.respondWith(caches.open(SHELL).then(async (cache) => {
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok && response.type === "basic") await cache.put(request, response.clone());
    return response;
  }));
});

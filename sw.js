// Service Worker: App startet auch offline. Kurs-APIs werden nie gecacht.
const CACHE = 'kr-shell-v1';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  // Netzwerk zuerst (immer aktuelle Version), bei fehlender Verbindung aus dem Cache
  e.respondWith(fetch(r).then(res => {
    const copy = res.clone();
    caches.open(CACHE).then(c => c.put(r, copy));
    return res;
  }).catch(() => caches.match(r).then(m => m || caches.match('index.html'))));
});

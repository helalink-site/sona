const CACHE = 'sona-v1';
const STATIC = [
  '/', '/index.html', '/manifest.json',
  'https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Inter:wght@300;400;500;600&display=swap',
  'https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/style.css'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Cache audio files for offline playback
  if (e.request.url.includes('/api/stream/') || e.request.url.includes('/api/download/')) {
    e.respondWith(
      caches.open('sona-audio').then(async cache => {
        const cached = await cache.match(e.request);
        if (cached) return cached;
        const res = await fetch(e.request);
        cache.put(e.request, res.clone());
        return res;
      })
    );
    return;
  }
  // Network first for API, cache first for static
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
  } else {
    e.respondWith(caches.match(e.request).then(cached => cached || fetch(e.request)));
  }
});

// Push notifications
self.addEventListener('push', e => {
  const data = e.data?.json() || {};
  e.waitUntil(self.registration.showNotification(data.title || 'Sona', {
    body: data.body || 'New music dropped 🎵',
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    data: { url: data.url || '/' },
    actions: [
      { action: 'play', title: '▶ Play Now' },
      { action: 'dismiss', title: 'Later' }
    ]
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'play') {
    e.waitUntil(clients.openWindow(e.notification.data.url));
  }
});

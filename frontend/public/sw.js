// O.D.I.N. - Service Worker for Push Notifications

const CACHE_NAME = 'odin-v1.3.59';

// Install event
self.addEventListener('install', (event) => {
  console.log('[SW] Installing service worker');
  self.skipWaiting();
});

// Activate event — clean up old caches before claiming clients
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating service worker');
  event.waitUntil(
    caches.keys().then(names =>
      Promise.all(
        names.filter(name => name !== CACHE_NAME).map(name => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

// Push event - receive push notification
self.addEventListener('push', (event) => {
  console.log('[SW] Push received');
  
  let data = {
    title: 'O.D.I.N. Alert',
    body: 'You have a new notification',
    icon: '/odin-icon-192.svg',
    badge: '/odin-icon-192.svg',
    tag: 'odin-alert',
    data: {}
  };
  
  if (event.data) {
    try {
      const payload = event.data.json();
      data = {
        title: payload.title || data.title,
        body: payload.body || payload.message || data.body,
        icon: payload.icon || data.icon,
        badge: payload.badge || data.badge,
        tag: payload.tag || `odin-${payload.alert_type || 'alert'}`,
        data: {
          url: payload.url || '/',
          alert_id: payload.alert_id,
          alert_type: payload.alert_type,
          printer_id: payload.printer_id,
          job_id: payload.job_id
        }
      };
    } catch (e) {
      console.error('[SW] Error parsing push data:', e);
      data.body = event.data.text();
    }
  }
  
  const options = {
    body: data.body,
    icon: data.icon,
    badge: data.badge,
    tag: data.tag,
    data: data.data,
    vibrate: [200, 100, 200],
    requireInteraction: data.data.alert_type === 'PRINT_FAILED',
    actions: [
      { action: 'view', title: 'View' },
      { action: 'dismiss', title: 'Dismiss' }
    ]
  };
  
  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// Notification click event
self.addEventListener('notificationclick', (event) => {
  console.log('[SW] Notification clicked:', event.action);
  
  event.notification.close();
  
  if (event.action === 'dismiss') {
    return;
  }
  
  // Determine URL to open
  let url = '/';
  const data = event.notification.data || {};
  
  if (data.url) {
    url = data.url;
  } else if (data.job_id) {
    url = '/jobs';
  } else if (data.printer_id) {
    url = '/printers';
  } else if (data.alert_type === 'SPOOL_LOW') {
    url = '/spools';
  }
  
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // Try to focus existing window
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.focus();
          client.navigate(url);
          return;
        }
      }
      // Open new window
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});

// Notification close event
self.addEventListener('notificationclose', (event) => {
  console.log('[SW] Notification closed');
});


// PWA fetch handler — cache app shell assets, network-first for API
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API calls: always network, never cache
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // App shell assets: stale-while-revalidate
  if (event.request.method === 'GET' && (
    url.pathname.endsWith('.js') ||
    url.pathname.endsWith('.css') ||
    url.pathname.endsWith('.svg') ||
    url.pathname.endsWith('.woff2') ||
    url.pathname === '/'
  )) {
    event.respondWith(
      caches.open(CACHE_NAME).then(cache =>
        cache.match(event.request).then(cached => {
          const fetchPromise = fetch(event.request).then(response => {
            if (response.ok) cache.put(event.request, response.clone());
            return response;
          }).catch(() => cached);
          return cached || fetchPromise;
        })
      )
    );
    return;
  }
});

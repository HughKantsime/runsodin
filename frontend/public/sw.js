// PrintFarm Scheduler - Service Worker for Push Notifications

const CACHE_NAME = 'printfarm-v1';

// Install event
self.addEventListener('install', (event) => {
  console.log('[SW] Installing service worker');
  self.skipWaiting();
});

// Activate event
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating service worker');
  event.waitUntil(clients.claim());
});

// Push event - receive push notification
self.addEventListener('push', (event) => {
  console.log('[SW] Push received');
  
  let data = {
    title: 'PrintFarm Alert',
    body: 'You have a new notification',
    icon: '/logo192.png',
    badge: '/badge72.png',
    tag: 'printfarm-alert',
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
        tag: payload.tag || `printfarm-${payload.alert_type || 'alert'}`,
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

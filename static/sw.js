/* QueueQ Service Worker — handles push notifications */

const CACHE = 'queueq-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      clients.claim(),
      caches.delete(CACHE), // clean old caches
    ])
  );
});

self.addEventListener('push', (event) => {
  let data = { title: 'QueueQ', body: 'Your table is ready!', url: '/' };

  try {
    if (event.data) {
      data = event.data.json();
    }
  } catch (e) {
    // fall back to defaults
  }

  const options = {
    body: data.body,
    icon: '/static/icon-192.png',
    badge: '/static/badge-72.png',
    vibrate: [200, 100, 200, 100, 300],
    tag: 'queueq-table-ready',
    renotify: true,
    requireInteraction: true,  // stays on screen until user taps
    data: { url: data.url },
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const urlToOpen = event.notification.data?.url || '/';

  event.waitUntil(
    (async () => {
      // Try to focus an existing window/tab with the same URL
      const allClients = await clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      });

      for (const client of allClients) {
        // Check if any client matches our target URL
        if (client.url === urlToOpen || client.url.startsWith(urlToOpen + '?')) {
          await client.focus();
          return;
        }
      }

      // Also check if any window shows the same restaurant or status page
      for (const client of allClients) {
        if (client.url.includes('/queue/') || client.url.includes('/status/')) {
          // Navigate existing window to the status page
          await client.navigate(urlToOpen);
          await client.focus();
          return;
        }
      }

      // Open new window
      await clients.openWindow(urlToOpen);
    })()
  );
});

'use strict';

// Offline support for the study guide. Bump the version in CACHE_NAME whenever
// a pre-cached file changes: serving is cache-first, so returning visitors
// only get new files when a new cache is installed.
const CACHE_PREFIX = 'geology-ch5-';
const CACHE_NAME = CACHE_PREFIX + 'v2';
const PRECACHE_URLS = [
  './',
  'index.html',
  'study.css',
  'app.js',
  'data.js',
  'data-2.js',
  'data-3.js',
  'data-4.js',
  'manifest.webmanifest',
  'icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names
        .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
        .map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

// Browsers reject a navigation answered with a response flagged as redirected,
// which a host that redirects index.html to the folder URL would leave in the cache.
function withoutRedirect(response) {
  if (!response.redirected) return response;
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(
    caches.open(CACHE_NAME)
      .then((cache) => cache.match(request))
      .then((cached) => (cached ? withoutRedirect(cached) : fetch(request)))
  );
});

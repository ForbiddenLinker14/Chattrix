// sw.js - Service Worker for Room Chat

const CACHE_NAME = "chat-cache-v3";

// ==================================================
// Install
// ==================================================
self.addEventListener("install", (event) => {
  console.log("⚡ Service Worker: Installed");
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        cache.addAll([
          "/",
          "/index.html",
          "/manifest.json",
          "/icons/icon-192.png",
          "/icons/icon-512.png",
        ])
      )
  );
  self.skipWaiting();
});

// ==================================================
// Activate
// ==================================================
self.addEventListener("activate", (event) => {
  console.log("⚡ Service Worker: Activated");
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
        )
      )
  );
});

// ==================================================
// Fetch
// ==================================================
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    caches
      .match(event.request)
      .then(
        (res) =>
          res ||
          fetch(event.request).catch(() => new Response("⚠️ Offline mode"))
      )
  );
});

// ==================================================
// Push Notifications
// ==================================================
self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  console.log("📩 Push event received:", data);

  event.waitUntil(
    (async () => {
      const allClients = await clients.matchAll({ includeUncontrolled: true });
      const isClientFocused = allClients.some((c) => c.focused);

      const title = data.title || "Chattrix";
      const roomLine = data.room ? `Room: ${data.room}` : "";
      const msgLine = (() => {
        if (!data.sender || !data.text) return "New message";
        if (data.text.includes("sent a file:")) return data.text;
        return `${data.sender}: ${data.text}`;
      })();
      const body = `${roomLine}\n${msgLine}`;

      if (!isClientFocused) {
        const options = {
          body,
          icon: "/icons/icon-192.png",
          badge: "/icons/icon-192.png",
          data: {
            url: data.url || `/chat/${data.room || ""}`,
            room: data.room || null,
          },
        };
        await self.registration.showNotification(title, options);
      } else {
        allClients.forEach((c) =>
          c.postMessage({
            type: "PUSH_MESSAGE",
            room: data.room || null,
            body,
            url: data.url || `/chat/${data.room || ""}`,
          })
        );
      }
    })()
  );
});

// ==================================================
// Notification Click
// ==================================================
self.addEventListener("notificationclick", (event) => {
  event.waitUntil(
    (async () => {
      event.notification.close();
      const allClients = await clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      if (allClients.length > 0) {
        const client = allClients[0];
        await client.focus();
        if (event.notification.data?.url) {
          await client.navigate(event.notification.data.url);
        }
      } else {
        await clients.openWindow(event.notification.data?.url || "/");
      }
    })()
  );
});

// ==================================================
// Background Sync: Resend Messages
// ==================================================
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-messages") {
    console.log("🔄 Background Sync triggered");
    event.waitUntil(sendPendingMessages());
  }
});

// Helper: resend stored messages
async function sendPendingMessages() {
  const db = await openDB();
  const tx = db.transaction("outbox", "readonly");
  const store = tx.objectStore("outbox");
  const all = await store.getAll();

  if (!all || !all.length) {
    console.log("📭 No pending messages");
    return;
  }

  console.log("📤 Resending", all.length, "messages");

  for (const msg of all) {
    try {
      // 🔄 Tell client(s) to resend via socket.io
      const allClients = await clients.matchAll({
        includeUncontrolled: true,
        type: "window",
      });
      if (allClients.length > 0) {
        allClients[0].postMessage({ type: "RESEND_MESSAGE", msg });
      }
    } catch (err) {
      console.error("❌ Failed to resend:", err, msg);
    }
  }
}

// ==================================================
// IndexedDB Helper
// ==================================================
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("chatDB", 1);
    req.onerror = (e) => reject(e);
    req.onsuccess = (e) => resolve(e.target.result);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains("outbox")) {
        db.createObjectStore("outbox", { keyPath: "id", autoIncrement: true });
      }
    };
  });
}

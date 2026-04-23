/**
 * Web Push subscription helpers.
 *
 * Flow: fetch VAPID public key from the bridge, ask the browser for permission,
 * subscribe via the service worker's PushManager, then POST the subscription
 * JSON back to /api/push/subscribe.
 */

export type PushState =
  | "unsupported"
  | "needs-permission"
  | "subscribed"
  | "denied"
  | "error";

function urlBase64ToArrayBuffer(base64: string): ArrayBuffer {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const buffer = new ArrayBuffer(raw.length);
  const view = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i++) view[i] = raw.charCodeAt(i);
  return buffer;
}

export function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export async function currentPushState(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) return "subscribed";
    if (Notification.permission === "granted") return "needs-permission";
    return "needs-permission";
  } catch {
    return "error";
  }
}

async function fetchPublicKey(): Promise<string> {
  const resp = await fetch("/api/push/public-key");
  if (!resp.ok) throw new Error(`public-key fetch failed: ${resp.status}`);
  const data = await resp.json();
  if (!data.public_key) throw new Error("public-key response missing field");
  return data.public_key;
}

export async function subscribeToPush(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";

  const permission =
    Notification.permission === "default"
      ? await Notification.requestPermission()
      : Notification.permission;
  if (permission === "denied") return "denied";
  if (permission !== "granted") return "needs-permission";

  const publicKey = await fetchPublicKey();
  const reg = await navigator.serviceWorker.ready;
  const existing = await reg.pushManager.getSubscription();
  const subscription =
    existing ||
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToArrayBuffer(publicKey),
    }));

  const resp = await fetch("/api/push/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription.toJSON()),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`subscribe backend failed (${resp.status}): ${detail}`);
  }
  return "subscribed";
}

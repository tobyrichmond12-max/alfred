"""Push notifications via ntfy (legacy) and Web Push (PWA).

Two transports coexist during the Sprint 11 migration:

1. ntfy (self-hosted, docker on port 8088, Tailscale at
   https://<jetson-tailscale-hostname>:8443, topic 'alfred'). Reached via push(),
   push_urgent(), push_routine().
2. Web Push (VAPID) to the PWA via pywebpush. Reached via push_web().
   Subscriptions are stored in /mnt/nvme/alfred/data/push_subscriptions.json
   (written by POST /api/push/subscribe on the bridge).

Priority scale (ntfy convention):
    1 = min, silent delivery
    2 = low, no sound
    3 = default
    4 = high, pops on lock screen
    5 = max, urgent, bypasses some Do Not Disturb

The ntfy path stays as a fallback until PWA push is validated on the user's phone.
"""
import json
import logging
import os
import urllib.error
import urllib.request

NTFY_URL = os.environ.get("NTFY_URL", "https://<jetson-tailscale-hostname>:8443")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "alfred")
REQUEST_TIMEOUT_S = 10

ALFRED_HOME = "/mnt/nvme/alfred"
VAPID_PRIVATE_PEM = os.path.join(ALFRED_HOME, "config", "vapid_private.pem")
VAPID_CONTACT = os.environ.get("VAPID_CONTACT", "mailto:<your-email>")
SUBSCRIPTIONS_PATH = os.path.join(ALFRED_HOME, "data", "push_subscriptions.json")

logger = logging.getLogger(__name__)


def push(message: str, priority: int = 3, tags: list = None, title: str = None) -> bool:
    """Send a notification to the alfred topic. Returns True on success, False on failure.

    message: body text.
    priority: 1 (silent) through 5 (urgent). 3 is default.
    tags: list of ntfy emoji shortcodes, e.g. ["calendar", "warning"].
    title: optional notification title.
    """
    if not message:
        return False
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {
        "Priority": str(priority),
        "Content-Type": "text/plain; charset=utf-8",
    }
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = ",".join(tags)
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def push_urgent(message: str, title: str = None) -> bool:
    """Priority 5 push with a warning tag. Use for time-sensitive alerts."""
    return push(message, priority=5, tags=["warning"], title=title)


def push_routine(message: str, title: str = None) -> bool:
    """Priority 2 push with a brain tag. Use for low-noise FYI nudges."""
    return push(message, priority=2, tags=["brain"], title=title)


def _load_subscriptions() -> list[dict]:
    try:
        with open(SUBSCRIPTIONS_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict) and s.get("endpoint")]
    return []


def _save_subscriptions(subs: list[dict]) -> None:
    os.makedirs(os.path.dirname(SUBSCRIPTIONS_PATH), exist_ok=True)
    tmp = SUBSCRIPTIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(subs, f, indent=2)
    os.replace(tmp, SUBSCRIPTIONS_PATH)


def push_web(message: str, title: str = "Alfred", tag: str = None) -> int:
    """Send a Web Push notification to every stored PWA subscription.

    Returns the number of deliveries that succeeded. Dead endpoints (410 Gone
    or 404) are pruned from the subscriptions file on the fly.
    """
    if not message:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush not installed, skipping web push")
        return 0
    if not os.path.exists(VAPID_PRIVATE_PEM):
        logger.warning("VAPID private key missing at %s", VAPID_PRIVATE_PEM)
        return 0

    subs = _load_subscriptions()
    if not subs:
        return 0

    payload = json.dumps({"title": title, "body": message, "tag": tag or ""})
    sent = 0
    stale: list[str] = []

    for sub in subs:
        endpoint = sub.get("endpoint")
        if not endpoint:
            continue
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_PEM,
                vapid_claims={"sub": VAPID_CONTACT},
                ttl=60 * 60 * 24,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None) if getattr(e, "response", None) else None
            if status in (404, 410):
                stale.append(endpoint)
            else:
                logger.warning("web push failed for %s: %s", endpoint[:80], e)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("web push transport error for %s: %s", endpoint[:80], e)

    if stale:
        remaining = [s for s in subs if s.get("endpoint") not in stale]
        _save_subscriptions(remaining)
        logger.info("pruned %d stale push subscriptions", len(stale))

    return sent


# ---- telegram (primary since phase 1) ---------------------------------------

_bot = None  # populated by main_telegram at startup
_telegram_state_path = os.path.join(ALFRED_HOME, "data", "telegram_notify_state.json")


def set_bot(bot) -> None:
    """Register the running TelegramBot for in-process push_telegram calls."""
    global _bot
    _bot = bot


def _get_bot():
    global _bot
    if _bot is not None:
        return _bot
    # Best-effort resurrection for out-of-process callers: look for a shared
    # owner.json and fire over the Telegram Bot API directly using the token
    # in env. This keeps cron scripts working without needing the running
    # bot in the same process.
    return None


def _direct_send(text: str, disable_notification: bool = False, high: bool = False) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_files = [
        "/var/lib/alfred/owner.json",
        os.path.join(ALFRED_HOME, "data", "owner.json"),
    ]
    owner = None
    for p in owner_files:
        try:
            with open(p) as f:
                data = json.load(f)
            owner = data.get("chat_id")
            if owner:
                break
        except (OSError, json.JSONDecodeError):
            continue
    if not token or not owner:
        return False
    body = f"[!] {text}" if high else text
    params = {
        "chat_id": int(owner),
        "text": body,
        "disable_web_page_preview": "true",
    }
    if disable_notification:
        params["disable_notification"] = "true"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def push_telegram(text: str, priority: str = "normal") -> bool:
    """Send a Telegram notification. Thread-safe.

    priority: "low" (silent), "normal", "high" (attention prefix).
    Prefers the running bot singleton, falls back to a direct Bot API call
    when invoked from a cron or one-off script.
    """
    if not text:
        return False
    bot = _get_bot()
    if bot is not None:
        try:
            return bot.send_notification(text, priority=priority)
        except Exception:
            logger.exception("push_telegram: bot.send_notification crashed")
            return False
    return _direct_send(
        text,
        disable_notification=(priority == "low"),
        high=(priority == "high"),
    )


import urllib.parse  # noqa: E402  kept local so top import block stays small


if __name__ == "__main__":
    ok = push("Alfred is alive", title="Test", priority=4, tags=["robot_face"])
    print("ntfy:", "ok" if ok else "failed")
    n = push_web("Alfred web push test", title="Alfred")
    print(f"web: {n} subscriber(s) delivered")
    ok_tg = push_telegram("Alfred is alive (telegram)", priority="normal")
    print("telegram:", "ok" if ok_tg else "failed")

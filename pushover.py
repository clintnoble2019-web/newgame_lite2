"""
NexGame Lite — Pushover notifications
Kage Software · 2026

Sends push notifications to Clint's phone when meaningful events
happen (new lead opt-in, first Whop subscriber, etc). Deliberately
narrow scope: one function, one third-party dependency (requests,
already in the app), and every failure mode is silent — a Pushover
outage cannot break the primary flow (lead capture, checkout).

SETUP:
  1. Sign up at pushover.net, install the app on your phone
  2. Grab your user key from the dashboard (top-right)
  3. Create an Application on pushover.net for this app -> get an API token
  4. Set two Railway env vars:
       PUSHOVER_USER_KEY = <your 30-char user key>
       PUSHOVER_APP_TOKEN = <the API token from your application>

  If either var is missing/empty, notify() is a silent no-op — the
  app runs fine without Pushover configured.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_TIMEOUT_SECONDS = 5   # short — we're calling this inline in a request
                        # handler, don't block the response for slow retries


def notify(title: str, message: str, priority: int = 0) -> bool:
    """Send a push notification. Returns True on success, False on any
    failure (unconfigured, network error, API rejection).

    priority: Pushover priority level.
        -2 = silent, -1 = quiet, 0 = normal, 1 = high, 2 = emergency
        Default 0 (normal) — new leads shouldn't wake you up, but
        should show up as a normal notification.

    Failure modes handled silently (returns False, does NOT raise):
      - Missing env vars (Pushover not configured)
      - Pushover API is down / slow (5s timeout)
      - Pushover API rejects the request (invalid key, quota exceeded)

    None of these should ever propagate up to the /api/leads endpoint —
    if Pushover breaks, we still capture the lead. That's the whole
    point of the primary flow.
    """
    user_key = os.environ.get("PUSHOVER_USER_KEY", "").strip()
    app_token = os.environ.get("PUSHOVER_APP_TOKEN", "").strip()

    if not user_key or not app_token:
        # Not configured — this is the expected state before setup,
        # not an error. Debug-log so it's visible if you're looking for
        # why notifications aren't arriving, but don't spam warn logs.
        logger.debug("Pushover not configured (PUSHOVER_USER_KEY or "
                     "PUSHOVER_APP_TOKEN missing) — skipping notify")
        return False

    try:
        response = requests.post(
            PUSHOVER_URL,
            data={
                "token": app_token,
                "user": user_key,
                "title": title,
                "message": message,
                "priority": priority,
            },
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return True
        logger.warning("Pushover API returned %s: %s",
                       response.status_code, response.text[:200])
        return False
    except requests.RequestException as e:
        logger.warning("Pushover request failed: %s", e)
        return False

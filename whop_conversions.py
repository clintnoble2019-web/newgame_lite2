"""
NexGame Lite — Whop Conversions API (server-side lead tracking)
Kage Software · 2026

Sends a "lead" conversion event directly to Whop's server-side API,
as a COMPLEMENT to the client-side whop.track('lead') pixel call
already firing in landing.html/trial.html — not a replacement.

Same pattern Meta itself recommends for its own Pixel + Conversions
API (CAPI): running both together catches conversions the browser-
side pixel alone can miss (ad blockers, Safari ITP, in-app browsers
stripping JS). Whop's own dashboard treats these as two separate,
complementary checklist items ("Is the pixel covering your ads?" and
"Track conversions from your server") — this fills in the second one.

CONFIRMED REAL (2026-07-22): the endpoint and payload shape below came
directly from a curl example Clint pulled from his own Whop dashboard's
"Track conversions from your server" panel — not fabricated or
guessed. An earlier, unverified version of this same idea (from an
unknown source) was NOT implemented specifically because its endpoint
couldn't be confirmed against any real Whop documentation at the time.

SETUP: reuses the existing WHOP_API_KEY (already configured for OAuth)
and WHOP_COMPANY_ID (defaults to the known real biz_ id already used in
the client-side pixel — not a secret, it's visible in page source).

Every failure mode here is silent by design — this must never block an
actual lead from saving. The client-side pixel is the primary,
already-confirmed-working path; this is a best-effort supplement.
"""

import logging
import os
from datetime import datetime, timezone

import requests

import config

logger = logging.getLogger(__name__)

CONVERSIONS_URL = "https://api.whop.com/api/v1/conversions"
_TIMEOUT_SECONDS = 5   # short — called inline during a request handler,
                        # don't block the response for a slow/dead API


def send_lead_event(email: str, url: str, ip_address: str = "",
                    user_agent: str = "", fbclid: str = "") -> bool:
    """Report a 'lead' conversion to Whop's server-side Conversions API.

    Returns True on success, False on any failure (unconfigured,
    network error, API rejection) — never raises. Call this AFTER a
    lead has already been saved to the DB; a failure here must not
    undo or block that save.

    ip_address/user_agent/fbclid are optional context that improve
    Whop's ability to match this event to the right ad click, per
    their own documented payload shape — omitted fields are simply
    left out of the request rather than sent as empty strings, since
    an empty fbclid could be worse for matching than no fbclid at all.
    """
    if not config.WHOP_API_KEY or not config.WHOP_COMPANY_ID:
        logger.debug("Whop Conversions API not configured "
                     "(WHOP_API_KEY or WHOP_COMPANY_ID missing) — skipping")
        return False

    user = {"email": email}
    context = {}
    if ip_address:
        context["ip_address"] = ip_address
    if user_agent:
        context["user_agent"] = user_agent
    if fbclid:
        context["fbclid"] = fbclid

    payload = {
        "event_name": "lead",
        "company_id": config.WHOP_COMPANY_ID,
        "event_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "url": url,
        "user": user,
    }
    if context:
        payload["context"] = context

    try:
        response = requests.post(
            CONVERSIONS_URL,
            headers={
                "Authorization": f"Bearer {config.WHOP_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code in (200, 201):
            return True
        logger.warning("Whop Conversions API returned %s: %s",
                       response.status_code, response.text[:200])
        return False
    except requests.RequestException as e:
        logger.warning("Whop Conversions API request failed: %s", e)
        return False

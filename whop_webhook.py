"""
NexGame Lite — Whop Webhook Receiver
Kage Software · 2026

Receives Whop's webhook events and feeds them into customers.py.
REPLACES gumroad_webhook.py entirely (2026-07-18) — Whop is now the
sole B2C payment processor.

SETUP AT LAUNCH:
    1. Deploy this alongside api/main.py (same FastAPI app)
    2. In Whop: Dashboard > Developer > Create Webhook
       -> URL: https://nexgamelite.com/webhooks/whop
       -> Events to select: membership.activated, membership.deactivated
       -> API version: v1
    3. Whop shows you a webhook_secret (format: whsec_xxxxx) ONE TIME
       at creation — copy it into WHOP_WEBHOOK_SECRET immediately.
    4. Use Whop's "Recent Deliveries" panel to send a real test event
       before going live — confirm the exact field names below against
       a real payload. Whop's docs describe the membership object as
       "one-to-one with the Membership object from Whop API V2, with
       AccessPass, Plan, and User expanded" but don't spell out every
       field name in the guide text alone — verify against a real
       delivery, same caution as the old Gumroad integration.

SIGNATURE VERIFICATION — Whop uses the open Standard Webhooks spec
(https://www.standardwebhooks.com/), confirmed via Whop's own docs:
    Headers: webhook-id, webhook-timestamp, webhook-signature
    Signed content: "{webhook-id}.{webhook-timestamp}.{raw_body}"
    Secret: the part after "whsec_" is base64-decoded to get the raw
            HMAC key bytes (Standard Webhooks convention)
    Signature header value looks like "v1,<base64 signature>" — may
    contain multiple space-separated signatures if the secret was
    recently rolled; check membership against ANY of them, not just
    the first.

EVENTS HANDLED — confirmed from Whop's official webhook docs:
    membership.activated    -> customer gained access (new sub OR
                                trial start) -> create/reactivate
    membership.deactivated  -> customer lost access (cancelled,
                                failed payment, or trial ended
                                without converting) -> mark cancelled

Whop also sends payment.succeeded, refund.created, dispute.created —
not handled here since membership.* already captures the access-state
transitions this app actually needs to act on. Add handlers for those
later if you want payment-level logging separate from access state.
"""

import base64
import hashlib
import hmac
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Request, HTTPException

import config
import customers as cust

router = APIRouter()

# Maps Whop's plan ID -> our Tier enum. The single product URL
# (whop.com/nexgame-lite/nexgame-lite-44) hosts all three billing
# intervals on Whop's side, but each still has its own plan_id under
# the hood — Whop tells us which one the customer picked via this
# field on the membership webhook payload.
#
# FILL THESE IN: get the three plan IDs from Whop Dashboard > Products
# > NexGame-Lite > Plans (three rows: monthly / 6-month / annual).
# Unknown plan_ids fall back to MONTHLY (see _tier_from_plan_id).
PLAN_ID_TIER_MAP = {
    "PASTE_MONTHLY_PLAN_ID_HERE": cust.Tier.MONTHLY,       # $39.98/mo
    "PASTE_SEMIANNUAL_PLAN_ID_HERE": cust.Tier.SEMIANNUAL, # $199.99/6mo
    "PASTE_ANNUAL_PLAN_ID_HERE": cust.Tier.ANNUAL,         # $399.99/yr
}


def _tier_from_plan_id(plan_id: str) -> cust.Tier:
    return PLAN_ID_TIER_MAP.get(plan_id, cust.Tier.MONTHLY)


def _verify_signature(raw_body: bytes, webhook_id: str, timestamp: str,
                      signature_header: str) -> bool:
    if not config.WHOP_WEBHOOK_SECRET:
        return True  # not configured yet — dev mode, skip verification

    secret = config.WHOP_WEBHOOK_SECRET
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_"):]
    key_bytes = base64.b64decode(secret)

    signed_content = f"{webhook_id}.{timestamp}.{raw_body.decode('utf-8')}"
    expected = base64.b64encode(
        hmac.new(key_bytes, signed_content.encode("utf-8"),
                 hashlib.sha256).digest()
    ).decode("utf-8")

    # signature_header may be "v1,<sig>" or multiple space-separated
    # "v1,<sig>" values if the secret was recently rolled.
    for part in signature_header.split():
        _, _, sig = part.partition(",")
        if hmac.compare_digest(sig, expected):
            return True
    return False


def _extract_membership_fields(data: dict) -> dict:
    """Pulls the fields this app actually needs off the membership
    object. Field names are best-effort based on Whop's documented
    object shape (Membership + expanded User/Plan) — CONFIRM against
    a real test delivery before going live; adjust the .get() paths
    here if Whop's actual payload nests differently."""
    user = data.get("user") or {}
    plan = data.get("plan") or {}
    return {
        "membership_id": data.get("id", ""),
        "email": user.get("email", "") or data.get("email", ""),
        "name": (user.get("username") or user.get("name")
                or (user.get("email", "").split("@")[0] if user.get("email") else "")),
        "status": data.get("status", ""),           # e.g. "trialing", "active"
        "plan_id": plan.get("id", ""),
    }


@router.post("/webhooks/whop")
async def whop_webhook(request: Request):
    raw = await request.body()
    webhook_id = request.headers.get("webhook-id", "")
    timestamp = request.headers.get("webhook-timestamp", "")
    signature = request.headers.get("webhook-signature", "")
    if not _verify_signature(raw, webhook_id, timestamp, signature):
        raise HTTPException(403, "Invalid webhook signature")

    body = await request.json()
    event = body.get("event", "")
    data = body.get("data", {})

    cust.init_db()
    fields = _extract_membership_fields(data)
    membership_id = fields["membership_id"]
    email = fields["email"]

    existing = (cust.get_by_whop_subscription(membership_id)
               if membership_id else None)

    # ── membership.activated: new subscriber OR trial start ─────────
    if event == "membership.activated":
        tier = _tier_from_plan_id(fields["plan_id"])
        # UPDATED 2026-07-21: confirmed reality is the OPPOSITE of what
        # this comment originally speculated — Monthly does NOT have a
        # trial (removed after the first week), while Semiannual and
        # Annual DO have a 7-day free trial. Whop reports "trialing"
        # for those two tiers during that window, "active" once it
        # converts to paid, or "active" immediately for Monthly (no
        # trial state to pass through at all).
        status = (cust.SubStatus.TRIALING if fields["status"] == "trialing"
                  else cust.SubStatus.ACTIVE)

        if existing:
            # Resubscribe / trial->paid conversion on an existing record
            cust.update_sub_status(existing.customer_id, status)
            return {"status": "reactivated", "sub_status": status.value}

        customer = cust.Customer(
            customer_id=str(uuid.uuid4())[:8],
            name=fields["name"] or email.split("@")[0],
            email=email,
            tier=tier,
            purchased_at=date.today().isoformat(),
            source="whop",
            whop_subscription_id=membership_id,
            sub_status=status,   # honored by add_customer per its docstring
        )
        cust.add_customer(customer)
        return {"status": "customer_created", "tier": tier.value,
               "sub_status": status.value}

    # ── membership.deactivated: cancelled, failed payment, or trial
    #    ended without converting ───────────────────────────────────
    if event == "membership.deactivated":
        if existing:
            cust.update_sub_status(existing.customer_id, cust.SubStatus.CANCELLED)
            return {"status": "marked_cancelled"}
        return {"status": "ignored", "reason": "deactivated event for "
                "unknown membership_id — no matching customer on file"}

    return {"status": "ignored", "reason": f"unhandled event type: {event}"}

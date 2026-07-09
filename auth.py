"""
NexGame Lite — Authentication
Kage Software · 2026

Real per-customer login, correct from day one (not a shared password).

Login credential = email + license_key.
    B2C (Gumroad): license_key comes from Gumroad's own license key
                   generation, captured by gumroad_webhook.py.
    B2B (Contra):  license_key is auto-generated when you add the
                   customer via manage_customers.py — give it to them
                   directly (email, DM, whatever channel you sold on).

Session = a signed, expiring cookie. No external auth library needed —
HMAC-SHA256 over customer_id + expiry, using SECRET_KEY from config.
Stateless: nothing to store server-side, nothing to clean up.
"""

import base64
import hashlib
import hmac
import json
import time

import config
import customers as cust

SESSION_COOKIE_NAME = "nexgame_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60   # 30 days


def _sign(payload: bytes) -> str:
    return hmac.new(config.SECRET_KEY.encode(), payload,
                    hashlib.sha256).hexdigest()


def create_session_token(customer_id: str) -> str:
    """Signed token: base64(json) + '.' + hmac signature."""
    body = json.dumps({
        "customer_id": customer_id,
        "expires": int(time.time()) + SESSION_TTL_SECONDS,
    }).encode()
    b64_body = base64.urlsafe_b64encode(body).decode()
    signature = _sign(b64_body.encode())
    return f"{b64_body}.{signature}"


def verify_session_token(token: str) -> str | None:
    """Returns customer_id if the token is valid and unexpired, else None."""
    if not token or "." not in token:
        return None
    b64_body, signature = token.rsplit(".", 1)
    expected = _sign(b64_body.encode())
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(b64_body.encode()))
    except Exception:
        return None
    if body.get("expires", 0) < time.time():
        return None
    return body.get("customer_id")


def login(email: str, license_key: str) -> tuple[str, cust.Customer] | None:
    """Verify credentials, confirm active access, return (token, customer)
    or None if login should be rejected."""
    customer = cust.verify_credentials(email, license_key)
    if not customer:
        return None
    if not cust.has_active_access(customer):
        return None   # correct credentials, but access has lapsed
    token = create_session_token(customer.customer_id)
    return token, customer


def get_current_customer(token: str) -> cust.Customer | None:
    """Used by protected routes: resolve a session cookie back to a
    Customer, re-checking access is still active (handles mid-session
    cancellations/expirations without waiting for token expiry)."""
    customer_id = verify_session_token(token)
    if not customer_id:
        return None
    customer = cust.get_customer(customer_id)
    if not customer or not cust.has_active_access(customer):
        return None
    return customer

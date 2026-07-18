"""
NexGame Lite — Customer Lifecycle Tests
Kage Software · 2026

Every test maps to a branch of the pricing/customer decision tree
locked during the Whop strategy conversation (2026-07-18, replacing
the earlier Gumroad-based structure).
Run: python tests/test_customers.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone

import customers as cust

TEST_DB = "test_customers_suite.db"


def days_ago(check_date: date, n: int) -> str:
    return (check_date - timedelta(days=n)).isoformat()


def dt_days_ago(check_date: date, n: int) -> str:
    base = datetime.combine(check_date, datetime.min.time(),
                            tzinfo=timezone.utc)
    return (base - timedelta(days=n)).isoformat(timespec="seconds")


class TestB2BBranch(unittest.TestCase):
    """Season / Lifetime — Contra clients."""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        cust.init_db(TEST_DB)
        self.check_date = date(2026, 11, 15)  # after locked SEASON_END

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_season_active_no_action(self):
        c = cust.Customer("c1", "Alex", "a@test.com", cust.Tier.SEASON,
                          "2026-07-01")
        cust.add_customer(c, TEST_DB)
        action = cust.determine_action(c, date(2026, 8, 1))
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_season_ending_soon(self):
        c = cust.Customer("c1", "Alex", "a@test.com", cust.Tier.SEASON,
                          "2026-07-01")
        cust.add_customer(c, TEST_DB)
        action = cust.determine_action(c, cust.SEASON_END - timedelta(days=5))
        self.assertEqual(action, cust.MessageAction.SEASON_ENDING_SOON)

    def test_season_ended_renewal_plea(self):
        c = cust.Customer("c1", "Alex", "a@test.com", cust.Tier.SEASON,
                          "2026-07-01")
        cust.add_customer(c, TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.RENEWAL_PLEA)

    def test_lifetime_thanks_after_season_end(self):
        c = cust.Customer("c2", "Jamie", "j@test.com", cust.Tier.LIFETIME,
                          "2026-07-01")
        cust.add_customer(c, TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.LIFETIME_THANKS)

    def test_lifetime_no_action_mid_season(self):
        c = cust.Customer("c2", "Jamie", "j@test.com", cust.Tier.LIFETIME,
                          "2026-07-01")
        cust.add_customer(c, TEST_DB)
        action = cust.determine_action(c, date(2026, 8, 1))
        self.assertEqual(action, cust.MessageAction.NONE)


class TestB2CBranch(unittest.TestCase):
    """Monthly/Semiannual/Annual — Whop subscribers.

    CHANGED 2026-07-18: Basic/Pro upgrade-nudge tests removed — that
    was a features-tier split that no longer exists (Whop sells one
    product across three billing intervals, not two feature tiers).
    Win-back tests carried over unchanged since that logic didn't
    depend on which of the old tiers a customer was on."""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        cust.init_db(TEST_DB)
        self.check_date = date(2026, 11, 15)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_active_monthly_no_action(self):
        c = cust.Customer("c1", "Sam", "s@test.com", cust.Tier.MONTHLY,
                          days_ago(self.check_date, 10), source="whop")
        cust.add_customer(c, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_active_annual_no_action(self):
        """Long tenure on any tier is a non-event now — there's no
        upgrade path left to nudge toward."""
        c = cust.Customer("c1", "Taylor", "t@test.com", cust.Tier.ANNUAL,
                          days_ago(self.check_date, 200), source="whop")
        cust.add_customer(c, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_trialing_has_access(self):
        c = cust.Customer("c1", "Jordan", "j@test.com", cust.Tier.MONTHLY,
                          days_ago(self.check_date, 2), source="whop")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.TRIALING, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        self.assertTrue(cust.has_active_access(c, self.check_date))

    def test_recent_cancellation_win_back(self):
        c = cust.Customer("c1", "Casey", "c@test.com", cust.Tier.MONTHLY,
                          days_ago(self.check_date, 70), source="whop")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.CANCELLED, TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        conn.execute("UPDATE customers SET status_updated_at=? "
                    "WHERE customer_id=?",
                    (dt_days_ago(self.check_date, 1), "c1"))
        conn.commit(); conn.close()
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.WIN_BACK)

    def test_old_cancellation_window_missed(self):
        c = cust.Customer("c1", "Morgan", "m@test.com", cust.Tier.SEMIANNUAL,
                          days_ago(self.check_date, 70), source="whop")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.CANCELLED, TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        conn.execute("UPDATE customers SET status_updated_at=? "
                    "WHERE customer_id=?",
                    (dt_days_ago(self.check_date, 10), "c1"))
        conn.commit(); conn.close()
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_win_back_sent_once_not_repeated(self):
        c = cust.Customer("c1", "Casey", "c@test.com", cust.Tier.MONTHLY,
                          days_ago(self.check_date, 70), source="whop")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.CANCELLED, TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        conn.execute("UPDATE customers SET status_updated_at=? "
                    "WHERE customer_id=?",
                    (dt_days_ago(self.check_date, 1), "c1"))
        conn.commit(); conn.close()

        cust._mark_contacted("c1", cust.MessageAction.WIN_BACK, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        self.assertTrue(c.win_back_sent)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_resubscribe_resets_win_back_flag(self):
        c = cust.Customer("c1", "Casey", "c@test.com", cust.Tier.MONTHLY,
                          days_ago(self.check_date, 70), source="whop")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.CANCELLED, TEST_DB)
        cust._mark_contacted("c1", cust.MessageAction.WIN_BACK, TEST_DB)
        self.assertTrue(cust.get_customer("c1", TEST_DB).win_back_sent)

        cust.update_sub_status("c1", cust.SubStatus.ACTIVE, TEST_DB)
        self.assertFalse(cust.get_customer("c1", TEST_DB).win_back_sent)

    def test_resubscribe_into_trialing_also_resets_win_back_flag(self):
        """A resubscribe often lands back in a trial state, not
        straight to active -- win_back_sent should reset either way."""
        c = cust.Customer("c1", "Riley", "r@test.com", cust.Tier.MONTHLY,
                          days_ago(self.check_date, 70), source="whop")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.CANCELLED, TEST_DB)
        cust._mark_contacted("c1", cust.MessageAction.WIN_BACK, TEST_DB)
        self.assertTrue(cust.get_customer("c1", TEST_DB).win_back_sent)

        cust.update_sub_status("c1", cust.SubStatus.TRIALING, TEST_DB)
        self.assertFalse(cust.get_customer("c1", TEST_DB).win_back_sent)


class TestWhopWebhook(unittest.TestCase):
    """Webhook receiver — activation creates/reactivates a customer on
    the right tier, deactivation marks them cancelled.

    Signature verification is skipped in these tests since
    WHOP_WEBHOOK_SECRET is unset in the test environment (see
    _verify_signature's dev-mode bypass) — matches how the old Gumroad
    tests worked the same way."""

    def setUp(self):
        if os.path.exists("nexgame_lite_customers.db"):
            os.remove("nexgame_lite_customers.db")
        from fastapi.testclient import TestClient
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists("nexgame_lite_customers.db"):
            os.remove("nexgame_lite_customers.db")
        if os.path.exists("nexgame_lite.db"):
            os.remove("nexgame_lite.db")

    def test_activation_creates_customer_on_mapped_tier(self):
        import whop_webhook
        # Use a real key from the map so the tier resolves correctly
        # regardless of what the placeholder values get changed to.
        plan_id = next(iter(whop_webhook.PLAN_ID_TIER_MAP))
        expected_tier = whop_webhook.PLAN_ID_TIER_MAP[plan_id]

        resp = self.client.post("/webhooks/whop", json={
            "event": "membership.activated",
            "data": {
                "id": "mem_test1",
                "status": "active",
                "user": {"email": "x@test.com", "username": "xtest"},
                "plan": {"id": plan_id},
            },
        })
        self.assertEqual(resp.status_code, 200)
        c = cust.get_by_whop_subscription("mem_test1")
        self.assertIsNotNone(c)
        self.assertEqual(c.tier, expected_tier)
        self.assertEqual(c.sub_status, cust.SubStatus.ACTIVE)

    def test_activation_with_trialing_status_sets_trialing(self):
        resp = self.client.post("/webhooks/whop", json={
            "event": "membership.activated",
            "data": {
                "id": "mem_test2",
                "status": "trialing",
                "user": {"email": "trial@test.com", "username": "trialuser"},
                "plan": {"id": "unmapped_plan_id"},
            },
        })
        self.assertEqual(resp.status_code, 200)
        c = cust.get_by_whop_subscription("mem_test2")
        self.assertEqual(c.sub_status, cust.SubStatus.TRIALING)
        # unmapped plan_id falls back to MONTHLY, not a crash
        self.assertEqual(c.tier, cust.Tier.MONTHLY)

    def test_deactivation_updates_status(self):
        self.client.post("/webhooks/whop", json={
            "event": "membership.activated",
            "data": {
                "id": "mem_test3", "status": "active",
                "user": {"email": "y@test.com", "username": "ytest"},
                "plan": {"id": "some_plan"},
            },
        })
        resp = self.client.post("/webhooks/whop", json={
            "event": "membership.deactivated",
            "data": {"id": "mem_test3"},
        })
        self.assertEqual(resp.status_code, 200)
        c = cust.get_by_whop_subscription("mem_test3")
        self.assertEqual(c.sub_status, cust.SubStatus.CANCELLED)

    def test_deactivation_for_unknown_membership_is_ignored_not_error(self):
        resp = self.client.post("/webhooks/whop", json={
            "event": "membership.deactivated",
            "data": {"id": "mem_never_seen"},
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ignored")


if __name__ == "__main__":
    unittest.main(verbosity=2)

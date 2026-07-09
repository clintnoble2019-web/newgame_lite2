"""
NexGame Lite — Customer Lifecycle Tests
Kage Software · 2026

Every test maps to a branch of the pricing/customer decision tree
locked during the Gumroad strategy conversation.
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
    """Monthly Basic/Pro — Gumroad subscribers."""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        cust.init_db(TEST_DB)
        self.check_date = date(2026, 11, 15)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_basic_short_tenure_no_nudge(self):
        c = cust.Customer("c1", "Sam", "s@test.com", cust.Tier.MONTHLY_BASIC,
                          days_ago(self.check_date, 10), source="gumroad")
        cust.add_customer(c, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_basic_long_tenure_upgrade_nudge(self):
        c = cust.Customer("c1", "Sam", "s@test.com", cust.Tier.MONTHLY_BASIC,
                          days_ago(self.check_date, 70), source="gumroad")
        cust.add_customer(c, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.UPGRADE_NUDGE)

    def test_pro_never_gets_upgrade_nudge(self):
        c = cust.Customer("c1", "Taylor", "t@test.com", cust.Tier.MONTHLY_PRO,
                          days_ago(self.check_date, 90), source="gumroad")
        cust.add_customer(c, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        action = cust.determine_action(c, self.check_date)
        self.assertEqual(action, cust.MessageAction.NONE)

    def test_recent_cancellation_win_back(self):
        c = cust.Customer("c1", "Casey", "c@test.com", cust.Tier.MONTHLY_BASIC,
                          days_ago(self.check_date, 70), source="gumroad")
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
        c = cust.Customer("c1", "Morgan", "m@test.com", cust.Tier.MONTHLY_PRO,
                          days_ago(self.check_date, 70), source="gumroad")
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
        c = cust.Customer("c1", "Casey", "c@test.com", cust.Tier.MONTHLY_BASIC,
                          days_ago(self.check_date, 70), source="gumroad")
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
        c = cust.Customer("c1", "Casey", "c@test.com", cust.Tier.MONTHLY_BASIC,
                          days_ago(self.check_date, 70), source="gumroad")
        cust.add_customer(c, TEST_DB)
        cust.update_sub_status("c1", cust.SubStatus.CANCELLED, TEST_DB)
        cust._mark_contacted("c1", cust.MessageAction.WIN_BACK, TEST_DB)
        self.assertTrue(cust.get_customer("c1", TEST_DB).win_back_sent)

        cust.update_sub_status("c1", cust.SubStatus.ACTIVE, TEST_DB)
        self.assertFalse(cust.get_customer("c1", TEST_DB).win_back_sent)

    def test_upgrade_tier_changes_basic_to_pro(self):
        c = cust.Customer("c1", "Sam", "s@test.com", cust.Tier.MONTHLY_BASIC,
                          days_ago(self.check_date, 5), source="gumroad")
        cust.add_customer(c, TEST_DB)
        cust.upgrade_tier("c1", cust.Tier.MONTHLY_PRO, TEST_DB)
        c = cust.get_customer("c1", TEST_DB)
        self.assertEqual(c.tier, cust.Tier.MONTHLY_PRO)


class TestGumroadWebhook(unittest.TestCase):
    """Webhook receiver — new sale creates customer, cancel updates status."""

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

    def test_new_sale_creates_basic_customer(self):
        resp = self.client.post("/webhooks/gumroad", data={
            "subscription_id": "sub_test1", "email": "x@test.com",
            "full_name": "X Test", "variants": "Basic — $19.99/mo",
            "cancelled": "false", "refunded": "false",
        })
        self.assertEqual(resp.status_code, 200)
        c = cust.get_by_gumroad_subscription("sub_test1")
        self.assertIsNotNone(c)
        self.assertEqual(c.tier, cust.Tier.MONTHLY_BASIC)
        self.assertEqual(c.sub_status, cust.SubStatus.ACTIVE)

    def test_cancel_ping_updates_status(self):
        self.client.post("/webhooks/gumroad", data={
            "subscription_id": "sub_test2", "email": "y@test.com",
            "full_name": "Y Test", "variants": "Pro — $39.99/mo",
            "cancelled": "false", "refunded": "false",
        })
        resp = self.client.post("/webhooks/gumroad", data={
            "subscription_id": "sub_test2", "email": "y@test.com",
            "cancelled": "true",
        })
        self.assertEqual(resp.status_code, 200)
        c = cust.get_by_gumroad_subscription("sub_test2")
        self.assertEqual(c.sub_status, cust.SubStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main(verbosity=2)

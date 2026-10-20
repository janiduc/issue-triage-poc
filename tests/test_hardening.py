"""Tests for the deployment safeguards (APP_ENV=production) and the extra redaction patterns."""
import os
import unittest

from app import create_app
from tests.test_triage import Base
from triage.db import DEMO_PASSWORD
from triage.redact import redact

STRONG_KEY = "k" * 48


class ProductionSettingsTests(Base):
    def tearDown(self):
        for k in ("APP_ENV", "SECRET_KEY", "ADMIN_PASSWORD", "SEED_DEMO_USERS"):
            os.environ.pop(k, None)
        super().tearDown()

    def fresh_db(self, name):
        os.environ["DB_PATH"] = os.path.join(self.tmp.name, name)

    # HD1: production refuses to start with the default or a short signing key
    def test_hd1_weak_secret_key_refused(self):
        os.environ["APP_ENV"] = "production"
        with self.assertRaises(RuntimeError):
            create_app()
        os.environ["SECRET_KEY"] = "short"
        with self.assertRaises(RuntimeError):
            create_app()

    # HD2: production has no demo accounts, a bootstrap admin, and a secure cookie
    def test_hd2_production_has_no_demo_accounts(self):
        os.environ.update(APP_ENV="production", SECRET_KEY=STRONG_KEY,
                          ADMIN_PASSWORD="a-long-admin-password")
        self.fresh_db("prod.db")
        app = create_app()
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        client = app.test_client()
        self.assertFalse(client.get("/api/public-config").get_json()["demo_accounts"])
        self.assertEqual(client.post("/api/login", json={"username": "lead", "password": DEMO_PASSWORD}).status_code, 401)
        self.assertEqual(client.post("/api/login", json={"username": "admin",
                                                         "password": "a-long-admin-password"}).status_code, 200)

    # HD3: without demo accounts, a missing or short admin password stops the first start
    def test_hd3_bootstrap_admin_needs_password(self):
        os.environ.update(APP_ENV="production", SECRET_KEY=STRONG_KEY)
        self.fresh_db("prod2.db")
        with self.assertRaises(RuntimeError):
            create_app()

    # HD4: in development the demo accounts exist and the sign-in page lists them
    def test_hd4_development_shows_demo_accounts(self):
        client = self.app.test_client()
        self.assertTrue(client.get("/api/public-config").get_json()["demo_accounts"])
        self.assertFalse(self.app.config["SESSION_COOKIE_SECURE"])


class MoreRedactionTests(unittest.TestCase):
    # HD5: token formats seen in real bug reports are removed (fake values built at run time)
    def test_hd5_more_secret_formats(self):
        github = "ghp_" + "a1B2" * 9
        fine_grained = "github_pat_" + "Z9y8" * 20
        jwt = "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + "." + "s1gnatureV4lue"
        key = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END " + "RSA PRIVATE KEY-----"
        text, n = redact(f"token {github} and {fine_grained}, header {jwt}\n{key}\nthanks")
        for secret in (github, fine_grained, jwt, "MIIEow"):
            self.assertNotIn(secret, text)
        self.assertEqual(n, 4)
        self.assertIn("thanks", text)

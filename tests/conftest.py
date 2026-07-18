"""
Test configuration.

IMPORTANT: this file runs at pytest COLLECTION time, before any test module
imports `app`, `database`, `models`, etc. That ordering matters: app.py
reads SECRET_KEY (and database.py reads DATABASE_URL) once, at first
import, and Python caches modules — reloading them mid-session to point at
a different database is fragile and easy to get subtly wrong. Setting the
real env vars here, before the first import happens anywhere, means every
module picks up the correct test configuration exactly once, the same way
it would in production, and no test ever touches your real DATABASE_URL.

Between tests, instead of a fresh database file per test (which WOULD
require re-running Alembic per test and reintroduce the reload problem),
the `client` fixture below just deletes all rows from the application tables.
Cheap on SQLite, fully order-independent, and keeps tests from leaking
data into each other.
"""

import os
import subprocess
import tempfile

import pytest

_TEST_DB_DIR = tempfile.mkdtemp(prefix="bashops_test_")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR, "test.db")
_TEST_DB_URL = f"sqlite:///{_TEST_DB_PATH}"

os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["SITE_URL"] = "http://testserver"
os.environ["PADDLE_WEBHOOK_SECRET"] = "paddle_test_secret"
for _var in (
    "PADDLE_API_KEY",
    "PADDLE_CLIENT_TOKEN",
    "PADDLE_RADAR_MONTHLY_PRICE_ID",
    "PADDLE_RADAR_ANNUAL_PRICE_ID",
    "PADDLE_MAINTAINER_MONTHLY_PRICE_ID",
    "PADDLE_MAINTAINER_ANNUAL_PRICE_ID",
    "ADMIN_EMAILS",
    "ADMIN_EMAIL",
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "EMAIL_FROM_NAME",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_OAUTH_REDIRECT_URI",
):
    os.environ.pop(_var, None)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_migration = subprocess.run(
    ["alembic", "upgrade", "head"],
    cwd=_PROJECT_ROOT,
    env=os.environ.copy(),
    capture_output=True,
    text=True,
)
if _migration.returncode != 0:
    raise RuntimeError(
        "Failed to migrate the test database via Alembic:\n"
        f"stdout: {_migration.stdout}\nstderr: {_migration.stderr}"
    )


@pytest.fixture()
def client():
    """A TestClient backed by the shared test database, with a clean slate
    (no users, no targets) at the start of every test."""
    from starlette.testclient import TestClient
    from database import SessionLocal
    from models import (
        DeveloperProfile,
        Event,
        MaintainerAnalysis,
        OpportunityFeedItem,
        Target,
        User,
        UserOpportunityInteraction,
    )

    db = SessionLocal()
    db.query(Event).delete()
    db.query(UserOpportunityInteraction).delete()
    db.query(OpportunityFeedItem).delete()
    db.query(Target).delete()
    db.query(MaintainerAnalysis).delete()
    db.query(DeveloperProfile).delete()
    db.query(User).delete()
    db.commit()
    db.close()

    import app as app_module

    return TestClient(app_module.app)

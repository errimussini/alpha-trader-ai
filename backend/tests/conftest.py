"""Fixture di test. Richiedono PostgreSQL (es. `docker compose up -d db`).

TEST_DATABASE_URL di default: postgresql+psycopg://alpha:alpha@localhost:5432/alpha_trader_test
"""
import os

os.environ.setdefault("DATABASE_URL", os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://alpha:alpha@localhost:5432/alpha_trader_test"))
os.environ["ENVIRONMENT"] = "test"
os.environ["MARKET_DATA_PROVIDER"] = "demo"
os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.entities import User  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    cfg = Config(os.path.join(HERE, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def client():
    return TestClient(app)


def _make_user(email: str, role: str, password: str = "password-12345") -> None:
    with SessionLocal() as s:
        s.add(User(email=email, hashed_password=hash_password(password), role=role))
        s.commit()


def _token(client, email: str, password: str = "password-12345") -> dict:
    r = client.post("/api/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin(client):
    _make_user("admin@example.com", "admin")
    return _token(client, "admin@example.com")


@pytest.fixture
def analyst(client):
    _make_user("analyst@example.com", "analyst")
    return _token(client, "analyst@example.com")


@pytest.fixture
def viewer(client):
    _make_user("viewer@example.com", "viewer")
    return _token(client, "viewer@example.com")

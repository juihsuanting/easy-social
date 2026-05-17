from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from easy_social import create_app
from easy_social.extensions import db


@pytest.fixture()
def app():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test",
                "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                "UPLOAD_FOLDER": str(Path(temp_dir) / "uploads"),
                "MEDIA_STORAGE_BACKEND": "local",
                "WTF_CSRF_ENABLED": False,
                "CAPTCHA_ENABLED": False,
            }
        )
        with app.app_context():
            db.create_all()
        yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, username: str, email: str | None = None, password: str = "password"):
    return client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": password,
        },
        follow_redirects=True,
    )


def login(client, username_or_email: str, password: str = "password"):
    return client.post(
        "/auth/login",
        data={"username_or_email": username_or_email, "password": password},
        follow_redirects=True,
    )


def logout(client):
    return client.post("/auth/logout", follow_redirects=True)

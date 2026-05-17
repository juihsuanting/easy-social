from __future__ import annotations

import json
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

from easy_social import create_app
from easy_social.auth import generate_captcha_text
from easy_social.extensions import db


# ── Unit tests ───────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_generate_captcha_text_default_length():
    text = generate_captcha_text()
    assert len(text) == 5


@pytest.mark.unit
def test_generate_captcha_text_custom_length():
    text = generate_captcha_text(length=8)
    assert len(text) == 8


@pytest.mark.unit
def test_generate_captcha_text_uses_allowed_chars():
    allowed = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    for _ in range(20):
        text = generate_captcha_text()
        assert all(c in allowed for c in text)


@pytest.mark.unit
def test_generate_captcha_text_is_random():
    results = {generate_captcha_text() for _ in range(10)}
    assert len(results) > 1


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.fixture()
def captcha_app():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-captcha",
                "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                "UPLOAD_FOLDER": str(Path(temp_dir) / "uploads"),
                "MEDIA_STORAGE_BACKEND": "local",
                "CAPTCHA_ENABLED": True,
            }
        )
        with app.app_context():
            db.create_all()
        yield app


@pytest.fixture()
def captcha_client(captcha_app):
    return captcha_app.test_client()


@pytest.mark.integration
def test_captcha_image_returns_png(captcha_client):
    response = captcha_client.get("/auth/captcha")
    assert response.status_code == 200
    assert response.content_type == "image/png"


@pytest.mark.integration
def test_captcha_sets_session_answer(captcha_client):
    with captcha_client.session_transaction() as sess:
        assert "captcha_answer" not in sess
    captcha_client.get("/auth/captcha")
    with captcha_client.session_transaction() as sess:
        assert "captcha_answer" in sess
        assert len(sess["captcha_answer"]) == 5


@pytest.mark.integration
def test_register_without_captcha_is_rejected(captcha_client):
    captcha_client.get("/auth/captcha")
    response = captcha_client.post(
        "/auth/register",
        data={"username": "alice", "email": "alice@example.com", "password": "password"},
        follow_redirects=True,
    )
    assert b"CAPTCHA" in response.data


@pytest.mark.integration
def test_register_with_wrong_captcha_is_rejected(captcha_client):
    captcha_client.get("/auth/captcha")
    response = captcha_client.post(
        "/auth/register",
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password",
            "captcha": "WRONG",
        },
        follow_redirects=True,
    )
    assert b"CAPTCHA" in response.data


@pytest.mark.integration
def test_register_with_correct_captcha_succeeds(captcha_client):
    captcha_client.get("/auth/captcha")
    with captcha_client.session_transaction() as sess:
        correct_answer = sess["captcha_answer"]
    response = captcha_client.post(
        "/auth/register",
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password",
            "captcha": correct_answer,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Feed" in response.data


@pytest.mark.integration
def test_captcha_cannot_be_reused(captcha_client):
    captcha_client.get("/auth/captcha")
    with captcha_client.session_transaction() as sess:
        correct_answer = sess["captcha_answer"]
    captcha_client.post(
        "/auth/register",
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password",
            "captcha": correct_answer,
        },
        follow_redirects=True,
    )
    captcha_client.post("/auth/logout", follow_redirects=True)
    response = captcha_client.post(
        "/auth/register",
        data={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password",
            "captcha": correct_answer,
        },
        follow_redirects=True,
    )
    assert b"CAPTCHA" in response.data


# ── E2E (Selenium) tests ──────────────────────────────────────────────────────

selenium = pytest.importorskip("selenium")

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server


@pytest.fixture(scope="module")
def ui_captcha_app():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-captcha-ui",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{temp_path / 'ui_captcha.sqlite'}",
                "UPLOAD_FOLDER": str(temp_path / "uploads"),
                "MEDIA_STORAGE_BACKEND": "local",
                "WTF_CSRF_ENABLED": False,
                "CAPTCHA_ENABLED": True,
            }
        )
        with app.app_context():
            db.create_all()
        yield app


@pytest.fixture(scope="module")
def live_captcha_server(ui_captcha_app):
    try:
        server = make_server("127.0.0.1", 0, ui_captcha_app, threaded=True)
    except SystemExit:
        pytest.skip("Selenium live server could not bind to a local port")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def _get_captcha_answer_via_http(browser, base_url: str) -> str:
    cookies = {c["name"]: c["value"] for c in browser.get_cookies()}
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(
        f"{base_url}/auth/captcha-answer",
        headers={"Cookie": cookie_str},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["answer"]


def set_field_value(browser, field, value: str):
    browser.execute_script(
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('input', { bubbles: true }));"
        "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
        field,
        value,
    )


def submit_form(browser, form):
    browser.execute_script(
        "arguments[0].requestSubmit ? arguments[0].requestSubmit() : arguments[0].submit();",
        form,
    )


@pytest.mark.ui
def test_user_can_register_with_captcha(browser, live_captcha_server):
    browser.get(f"{live_captcha_server}/auth/register")
    WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.ID, "captcha-img"))
    )
    WebDriverWait(browser, 10).until(
        lambda d: d.execute_script("return document.getElementById('captcha-img').complete")
    )

    captcha_answer = _get_captcha_answer_via_http(browser, live_captcha_server)

    form = browser.find_element(By.CSS_SELECTOR, "form.form-stack")
    set_field_value(browser, form.find_element(By.NAME, "username"), "captchauser")
    set_field_value(browser, form.find_element(By.NAME, "email"), "captchauser@example.com")
    set_field_value(browser, form.find_element(By.NAME, "password"), "password")
    set_field_value(browser, form.find_element(By.NAME, "captcha"), captcha_answer)
    submit_form(browser, form)

    WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "form.composer"))
    )
    assert "Feed" in browser.find_element(By.TAG_NAME, "body").text

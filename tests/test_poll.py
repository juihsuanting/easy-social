from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from easy_social import create_app
from easy_social.extensions import db
from easy_social.models import PollOption, PollVote, Post, User

from conftest import login, logout, register


# ── Unit tests ───────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_poll_option_belongs_to_post(app):
    with app.app_context():
        user = User(username="alice", email="alice@example.com")
        user.set_password("password")
        db.session.add(user)
        db.session.flush()
        post = Post(body="Which colour?", is_poll=True, author=user)
        db.session.add(post)
        db.session.flush()
        opt = PollOption(post=post, text="Red", position=1)
        db.session.add(opt)
        db.session.commit()
        assert opt.post_id == post.id
        assert opt.text == "Red"


@pytest.mark.unit
def test_poll_vote_uniqueness_constraint(app):
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        user = User(username="bob", email="bob@example.com")
        user.set_password("password")
        db.session.add(user)
        db.session.flush()
        post = Post(body="Yes or No?", is_poll=True, author=user)
        db.session.add(post)
        db.session.flush()
        opt = PollOption(post=post, text="Yes", position=1)
        db.session.add(opt)
        db.session.flush()

        vote1 = PollVote(post=post, option=opt, user_id=user.id)
        db.session.add(vote1)
        db.session.commit()

        vote2 = PollVote(post=post, option=opt, user_id=user.id)
        db.session.add(vote2)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


@pytest.mark.unit
def test_poll_vote_count(app):
    with app.app_context():
        user1 = User(username="u1", email="u1@example.com")
        user2 = User(username="u2", email="u2@example.com")
        user1.set_password("pw")
        user2.set_password("pw")
        db.session.add_all([user1, user2])
        db.session.flush()
        post = Post(body="Q?", is_poll=True, author=user1)
        db.session.add(post)
        db.session.flush()
        opt = PollOption(post=post, text="A", position=1)
        db.session.add(opt)
        db.session.flush()
        db.session.add(PollVote(post=post, option=opt, user_id=user1.id))
        db.session.add(PollVote(post=post, option=opt, user_id=user2.id))
        db.session.commit()
        db.session.refresh(opt)
        assert len(opt.votes) == 2


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.integration
def test_create_poll_post(client, app):
    register(client, "alice")
    response = client.post(
        "/posts",
        data={
            "body": "Favourite colour?",
            "poll_option_1": "Red",
            "poll_option_2": "Blue",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        post = Post.query.filter_by(body="Favourite colour?").one()
        assert post.is_poll
        assert len(post.poll_options) == 2
        assert post.poll_options[0].text == "Red"
        assert post.poll_options[1].text == "Blue"


@pytest.mark.integration
def test_poll_requires_at_least_two_options(client, app):
    register(client, "alice")
    response = client.post(
        "/posts",
        data={"body": "Question?", "poll_option_1": "Only one"},
        follow_redirects=True,
    )
    with app.app_context():
        assert Post.query.filter_by(body="Question?", is_poll=True).count() == 0


@pytest.mark.integration
def test_vote_on_poll(client, app):
    register(client, "alice")
    client.post(
        "/posts",
        data={"body": "Cats or Dogs?", "poll_option_1": "Cats", "poll_option_2": "Dogs"},
        follow_redirects=True,
    )
    logout(client)
    register(client, "bob")

    with app.app_context():
        post = Post.query.filter_by(body="Cats or Dogs?").one()
        opt = post.poll_options[0]

    response = client.post(
        f"/posts/{post.id}/vote",
        data={"option_id": opt.id},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        vote = PollVote.query.filter_by(post_id=post.id).one()
        assert vote.option_id == opt.id


@pytest.mark.integration
def test_duplicate_vote_rejected(client, app):
    register(client, "alice")
    client.post(
        "/posts",
        data={"body": "Again?", "poll_option_1": "Yes", "poll_option_2": "No"},
        follow_redirects=True,
    )
    logout(client)
    register(client, "bob")

    with app.app_context():
        post = Post.query.filter_by(body="Again?").one()
        opt = post.poll_options[0]

    client.post(f"/posts/{post.id}/vote", data={"option_id": opt.id}, follow_redirects=True)
    response = client.post(
        f"/posts/{post.id}/vote",
        data={"option_id": opt.id},
        follow_redirects=True,
    )
    assert b"already voted" in response.data
    with app.app_context():
        assert PollVote.query.filter_by(post_id=post.id).count() == 1


@pytest.mark.integration
def test_poll_shows_results_after_voting(client, app):
    register(client, "alice")
    client.post(
        "/posts",
        data={"body": "Show results?", "poll_option_1": "Yes", "poll_option_2": "No"},
        follow_redirects=True,
    )
    logout(client)
    register(client, "bob")

    with app.app_context():
        post = Post.query.filter_by(body="Show results?").one()
        opt = post.poll_options[0]

    client.post(f"/posts/{post.id}/vote", data={"option_id": opt.id}, follow_redirects=True)
    response = client.get("/explore")
    assert b"100.0%" in response.data or b"100%" in response.data


# ── E2E (Selenium) tests ──────────────────────────────────────────────────────

selenium = pytest.importorskip("selenium")

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server


@pytest.fixture(scope="module")
def poll_ui_app():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        temp_path = Path(temp_dir)
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-poll-ui",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{temp_path / 'poll_ui.sqlite'}",
                "UPLOAD_FOLDER": str(temp_path / "uploads"),
                "MEDIA_STORAGE_BACKEND": "local",
                "WTF_CSRF_ENABLED": False,
                "CAPTCHA_ENABLED": False,
            }
        )
        with app.app_context():
            db.create_all()
        yield app


@pytest.fixture(scope="module")
def poll_live_server(poll_ui_app):
    try:
        server = make_server("127.0.0.1", 0, poll_ui_app, threaded=True)
    except SystemExit:
        pytest.skip("Selenium live server could not bind to a local port")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def clean_poll_db(poll_ui_app):
    with poll_ui_app.app_context():
        db.session.query(PollVote).delete()
        db.session.query(PollOption).delete()
        from easy_social.models import Comment
        db.session.query(Comment).delete()
        db.session.query(Post).delete()
        db.session.query(User).delete()
        db.session.commit()


def _set_field(browser, field, value):
    browser.execute_script(
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
        field, value,
    )


def _submit(browser, form):
    browser.execute_script(
        "arguments[0].requestSubmit ? arguments[0].requestSubmit() : arguments[0].submit();",
        form,
    )


def _register_ui(browser, base_url, username):
    browser.get(f"{base_url}/auth/register")
    form = WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "form.form-stack"))
    )
    _set_field(browser, form.find_element(By.NAME, "username"), username)
    _set_field(browser, form.find_element(By.NAME, "email"), f"{username}@example.com")
    _set_field(browser, form.find_element(By.NAME, "password"), "password")
    _submit(browser, form)
    WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "form.composer"))
    )


def _logout_ui(browser):
    _submit(browser, browser.find_element(By.CSS_SELECTOR, "header form"))
    WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.NAME, "username_or_email"))
    )


@pytest.mark.ui
def test_create_poll_and_vote_via_ui(browser, poll_live_server, clean_poll_db):
    _register_ui(browser, poll_live_server, "alice")

    poll_toggle = browser.find_element(By.ID, "poll-toggle")
    poll_toggle.click()

    composer = browser.find_element(By.CSS_SELECTOR, "form.composer")
    _set_field(browser, composer.find_element(By.NAME, "body"), "Best season?")
    _set_field(browser, composer.find_element(By.NAME, "poll_option_1"), "Spring")
    _set_field(browser, composer.find_element(By.NAME, "poll_option_2"), "Autumn")
    _submit(browser, composer)

    WebDriverWait(browser, 10).until(
        EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Best season?")
    )
    assert "Spring" in browser.find_element(By.TAG_NAME, "body").text

    _logout_ui(browser)
    _register_ui(browser, poll_live_server, "bob")

    vote_btn = WebDriverWait(browser, 10).until(
        EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'poll-option-btn') and text()='Spring']"))
    )
    vote_btn.click()

    WebDriverWait(browser, 10).until(
        EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "%")
    )
    assert "100" in browser.find_element(By.TAG_NAME, "body").text

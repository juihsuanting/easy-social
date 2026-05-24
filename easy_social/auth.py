from __future__ import annotations

import hashlib
import hmac as _hmac
import io
import secrets

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db
from .models import User

bp = Blueprint("auth", __name__, url_prefix="/auth")

_CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_captcha_text(length: int = 5) -> str:
    return "".join(secrets.choice(_CAPTCHA_CHARS) for _ in range(length))


def _captcha_hmac(text: str) -> str:
    return _hmac.new(
        current_app.config["SECRET_KEY"].encode(),
        text.upper().encode(),
        hashlib.sha256,
    ).hexdigest()


@bp.route("/captcha")
def captcha():
    from captcha.image import ImageCaptcha

    text = generate_captcha_text()
    session["captcha_hash"] = _captcha_hmac(text)
    if current_app.config.get("TESTING"):
        session["captcha_answer"] = text
    try:
        image = ImageCaptcha()
        data = image.generate(text)
    except Exception as exc:
        current_app.logger.error("CAPTCHA generation failed: %s", exc)
        return "CAPTCHA unavailable", 503
    response = send_file(io.BytesIO(data.read()), mimetype="image/png")
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/captcha-answer")
def captcha_answer():
    if not current_app.config.get("TESTING"):
        return "Not found", 404
    return jsonify({"answer": session.get("captcha_answer")})


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("social.feed"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        error = None

        if current_app.config.get("CAPTCHA_ENABLED", True):
            captcha_input = request.form.get("captcha", "").strip().upper()
            stored_hash = session.pop("captcha_hash", None)
            session.pop("captcha_answer", None)
            if not captcha_input:
                error = "Please complete the CAPTCHA."
            elif not stored_hash or not _hmac.compare_digest(
                _captcha_hmac(captcha_input), stored_hash
            ):
                error = "CAPTCHA is incorrect. Please try again."

        if not error:
            if not username or not email or not password:
                error = "Username, email, and password are required."
            elif len(username) > 40:
                error = "Username must be 40 characters or fewer."
            elif User.query.filter_by(username=username).first():
                error = "That username is already taken."
            elif User.query.filter_by(email=email).first():
                error = "That email is already registered."

        if error:
            flash(error, "error")
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("social.feed"))

    return render_template("auth/register.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("social.feed"))

    if request.method == "POST":
        username_or_email = request.form.get("username_or_email", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(
            (User.username == username_or_email)
            | (User.email == username_or_email.lower())
        ).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("social.feed"))

        flash("Invalid username/email or password.", "error")

    return render_template("auth/login.html")


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))

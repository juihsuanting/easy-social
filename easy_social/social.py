from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from flask_login import current_user, login_required
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import joinedload

from .extensions import db
from .media import save_media
from .models import Comment, PollOption, PollVote, Post, User, followers

bp = Blueprint("social", __name__)


def _post_query():
    return Post.query.options(
        joinedload(Post.author),
        joinedload(Post.repost_of).joinedload(Post.author),
        joinedload(Post.poll_options).joinedload(PollOption.votes),
        joinedload(Post.repost_of).joinedload(Post.poll_options).joinedload(PollOption.votes),
    )


def _comment_counts_for_posts(posts: list[Post]) -> dict[int, int]:
    post_ids = {post.display_post.id for post in posts}
    if not post_ids:
        return {}

    counts = dict.fromkeys(post_ids, 0)
    rows = (
        db.session.query(Comment.post_id, func.count(Comment.id))
        .filter(Comment.post_id.in_(post_ids))
        .group_by(Comment.post_id)
        .all()
    )
    counts.update({post_id: count for post_id, count in rows})
    return counts


def _poll_votes_for_posts(posts: list[Post]) -> dict[int, int]:
    poll_post_ids = [p.display_post.id for p in posts if p.display_post.is_poll]
    if not poll_post_ids:
        return {}
    rows = (
        db.session.query(PollVote.post_id, PollVote.option_id)
        .filter(PollVote.user_id == current_user.id, PollVote.post_id.in_(poll_post_ids))
        .all()
    )
    return {post_id: option_id for post_id, option_id in rows}


def _followed_user_ids(users: list[User]) -> set[int]:
    user_ids = [user.id for user in users]
    if not user_ids:
        return set()

    return {
        followed_id
        for (followed_id,) in db.session.query(followers.c.followed_id)
        .filter(
            followers.c.follower_id == current_user.id,
            followers.c.followed_id.in_(user_ids),
        )
        .all()
    }


@bp.route("/")
@login_required
def feed():
    followed_ids = db.session.query(followers.c.followed_id).filter(
        followers.c.follower_id == current_user.id
    )
    posts = (
        _post_query()
        .filter(or_(Post.author_id == current_user.id, Post.author_id.in_(followed_ids)))
        .order_by(desc(Post.created_at))
        .limit(100)
        .all()
    )
    return render_template(
        "social/feed.html",
        posts=posts,
        comment_counts=_comment_counts_for_posts(posts),
        poll_votes=_poll_votes_for_posts(posts),
    )


@bp.route("/explore")
@login_required
def explore():
    posts = _post_query().order_by(desc(Post.created_at)).limit(100).all()
    users = User.query.filter(User.id != current_user.id).order_by(User.username).limit(50).all()
    return render_template(
        "social/explore.html",
        posts=posts,
        users=users,
        comment_counts=_comment_counts_for_posts(posts),
        followed_user_ids=_followed_user_ids(users),
        poll_votes=_poll_votes_for_posts(posts),
    )


@bp.post("/posts")
@login_required
def create_post():
    body = request.form.get("body", "").strip()

    option_texts = [
        request.form.get(f"poll_option_{i}", "").strip()
        for i in range(1, 5)
    ]
    option_texts = [t for t in option_texts if t]
    is_poll = len(option_texts) >= 2

    if is_poll and not body:
        flash("Please add a question for your poll.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    if is_poll and len(option_texts) > 4:
        flash("A poll can have at most 4 options.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    try:
        media_filename, media_type = save_media(request.files.get("media"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("social.feed"))

    if not body and not media_filename and not is_poll:
        flash("Add text, an image, or a video before posting.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    post = Post(
        body=body,
        media_filename=media_filename,
        media_type=media_type,
        is_poll=is_poll,
        author=current_user,
    )
    db.session.add(post)
    db.session.flush()

    for i, text in enumerate(option_texts, start=1):
        db.session.add(PollOption(post=post, text=text, position=i))

    db.session.commit()
    return redirect(url_for("social.feed"))


@bp.post("/posts/<int:post_id>/vote")
@login_required
def vote_poll(post_id: int):
    post = db.get_or_404(Post, post_id)
    if not post.is_poll:
        flash("This post is not a poll.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    if post.author_id == current_user.id:
        flash("You cannot vote on your own poll.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    option_id = request.form.get("option_id", type=int)
    option = PollOption.query.filter_by(id=option_id, post_id=post.id).first_or_404()

    if PollVote.query.filter_by(user_id=current_user.id, post_id=post.id).first():
        flash("You have already voted on this poll.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    try:
        db.session.add(PollVote(post=post, option=option, user_id=current_user.id))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("You have already voted on this poll.", "error")
    return redirect(request.referrer or url_for("social.feed"))


@bp.get("/posts/<int:post_id>")
@login_required
def post_detail(post_id: int):
    post = _post_query().filter(Post.id == post_id).first_or_404()
    comments = post.comments.order_by(Comment.created_at.asc()).all()
    return render_template(
        "social/post_detail.html",
        post=post,
        comments=comments,
        comment_counts={post.display_post.id: len(comments)},
        poll_votes=_poll_votes_for_posts([post]),
    )


@bp.post("/posts/<int:post_id>/comments")
@login_required
def add_comment(post_id: int):
    post = db.get_or_404(Post, post_id)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment cannot be empty.", "error")
    else:
        db.session.add(Comment(body=body, author=current_user, post=post))
        db.session.commit()
    return redirect(url_for("social.post_detail", post_id=post.id))


@bp.post("/posts/<int:post_id>/repost")
@login_required
def repost(post_id: int):
    original = db.get_or_404(Post, post_id).display_post
    if original.author_id == current_user.id:
        flash("You cannot repost your own post.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    existing = Post.query.filter_by(author_id=current_user.id, repost_of_id=original.id).first()
    if existing:
        flash("You already reposted this.", "error")
        return redirect(request.referrer or url_for("social.feed"))

    db.session.add(Post(author=current_user, repost_of=original))
    db.session.commit()
    return redirect(request.referrer or url_for("social.feed"))


@bp.route("/users/<username>")
@login_required
def profile(username: str):
    user = User.query.filter_by(username=username).first_or_404()
    posts = (
        _post_query()
        .filter(Post.author_id == user.id)
        .order_by(desc(Post.created_at))
        .all()
    )
    return render_template(
        "social/profile.html",
        profile_user=user,
        posts=posts,
        comment_counts=_comment_counts_for_posts(posts),
        poll_votes=_poll_votes_for_posts(posts),
    )


@bp.post("/users/<username>/follow")
@login_required
def follow(username: str):
    user = User.query.filter_by(username=username).first_or_404()
    current_user.follow(user)
    db.session.commit()
    return redirect(request.referrer or url_for("social.profile", username=user.username))


@bp.post("/users/<username>/unfollow")
@login_required
def unfollow(username: str):
    user = User.query.filter_by(username=username).first_or_404()
    current_user.unfollow(user)
    db.session.commit()
    return redirect(request.referrer or url_for("social.profile", username=user.username))

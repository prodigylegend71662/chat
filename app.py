"""C.S.P. (Common Support Program) Chat application.

Single-instance Flask + Socket.IO app intended for Render's small free service.
The application uses PostgreSQL in production and has a controlled fallback chain.
"""
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from gevent import monkey
monkey.patch_all()

import bleach
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFError, validate_csrf
from flask_socketio import emit, join_room, leave_room
from sqlalchemy import select
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired


from ai_router import get_response
from config import Config
from db_fallback import database_status, init_db_with_fallback
from extensions import csrf, db, login_manager, socketio
from models import CalendarEvent, DirectMessage, InviteCode, Message, Reaction, Room, RoomNickname, User, UserReadReceipt, UserSettings
from profanity_words import PROFANITY_WORDS

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{1,28}[A-Za-z0-9]$")
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{3,20})")
ALLOWED_REACTIONS = {"👍", "❤️", "😂", "😮", "😢", "😡", "🎉", "🔥"}
MAX_MESSAGE_LENGTH = 1000
MESSAGE_INTERVAL = 0.5
EDIT_INTERVAL = 5.0
TYPING_INTERVAL = 2.0
SESSION_TIMEOUT = 30 * 60
STATUS_IDLE = 5 * 60
AVATARS = ["😀", "😎", "🤓", "🦊", "🐼", "🐸", "🐧", "🐨", "🐯", "🦁", "🐻", "🐰", "🐱", "🐶", "🐵", "🦄", "🐙", "🦋", "🐝", "🐢", "🌟", "⭐", "🚀", "🎮", "💻", "🔭", "🎨", "🎵", "📚", "🛠️", "❤️", "💙", "💚", "💜", "🧡", "🩵", "🌈", "🍀", "🌻", "🍎", "🍕", "☕", "⚡", "🔥", "❄️", "🌙", "☀️", "🤖", "👾"]
COLORS = ["#4f8cff", "#8b5cf6", "#14b8a6", "#f59e0b", "#ef4444", "#ec4899", "#22c55e", "#06b6d4"]
ONLINE = {}  # sid -> {user_id, username}
LAST_MESSAGE_AT = {}
LAST_EDIT_AT = {}
LAST_TYPING_AT = {}
TYPING = {}  # room -> {user_id: monotonic_time}
MANUAL_STATUS = {}  # user_id -> online/away/dnd/invisible


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


class SignupForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired()])
    submit = SubmitField("Create account")


def utcnow():
    return datetime.now(timezone.utc)


def normalize_datetime(value):
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_username(username):
    if not USERNAME_RE.fullmatch(username or ""):
        return "Username must be 3-20 characters using only letters, numbers, and underscores."
    return None


def validate_password(password):
    if len(password or "") < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[A-Za-z]", password):
        return "Password must contain at least one letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one number."
    return None


def sanitize_text(text, limit=MAX_MESSAGE_LENGTH):
    """Strip HTML and dangerous markup before persistence."""
    text = (text or "")[:limit]
    return bleach.clean(text, tags=[], attributes={}, strip=True).replace("\r\n", "\n").replace("\r", "\n").strip()


def filter_profanity(text, enabled=True):
    if not enabled:
        return text
    def repl(match):
        word = match.group(0)
        return word[0] + "*" * (len(word) - 1)
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, PROFANITY_WORDS)) + r")\b", re.IGNORECASE)
    return pattern.sub(repl, text)


def avatar_for(user):
    return user.avatar or (user.username[0].upper() if user.username else "?")


def display_name(user):
    settings = db.session.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    return (settings.display_name.strip() if settings and settings.display_name else user.username)


def user_public(user):
    status = MANUAL_STATUS.get(user.id, user.status or "offline")
    if status in {"dnd", "invisible"}:
        visible_status = "offline"
    else:
        last_active = normalize_datetime(user.last_active or utcnow())
        active = (utcnow() - last_active).total_seconds()
        visible_status = "away" if active >= STATUS_IDLE else status
    return {"id": user.id, "username": user.username, "display_name": display_name(user), "avatar": avatar_for(user), "status": visible_status, "role": user.role}


def room_key(room_id):
    return f"room:{room_id}"


def dm_key(a, b):
    lo, hi = sorted((int(a), int(b)))
    return f"dm_{lo}_{hi}"


def valid_room_access(user, room):
    return room is not None and user.is_authenticated and not user.banned


def get_dm_partner(key, user_id):
    m = re.fullmatch(r"dm_(\d+)_(\d+)", key or "")
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if user_id not in (a, b):
        return None
    return b if user_id == a else a


def can_user_send(user):
    return user.role not in {"guest", "bot"} and not user.banned


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapped


def socket_csrf_ok(data):
    token = (data or {}).get("csrf_token")
    if not token:
        return False
    try:
        validate_csrf(token)
        return True
    except Exception:
        return False


def serialize_message(message):
    user = db.session.get(User, message.user_id) if message.user_id else None
    reactions = {}
    if message.id:
        rows = db.session.scalars(select(Reaction).where(Reaction.message_id == message.id)).all()
        for row in rows:
            item = reactions.setdefault(row.emoji, {"count": 0, "mine": False, "users": []})
            item["count"] += 1
            if row.user_id == current_user.id:
                item["mine"] = True
            reactor = db.session.get(User, row.user_id)
            if reactor:
                item["users"].append(reactor.username)
    reply = None
    if message.reply_to:
        original = db.session.get(Message, message.reply_to)
        if original:
            original_user = db.session.get(User, original.user_id) if original.user_id else None
            reply = {"id": original.id, "username": original_user.username if original_user else "Deleted user", "text": original.text[:50], "created_at": normalize_datetime(original.created_at).isoformat()}
    return {
        "id": message.id,
        "room": message.room,
        "user_id": message.user_id,
        "username": user.username if user else "Deleted user",
        "display_name": display_name(user) if user else "Deleted user",
        "avatar": avatar_for(user) if user else "?",
        "role": user.role if user else "member",
        "text": "[message deleted]" if message.is_deleted else message.text,
        "created_at": normalize_datetime(message.created_at).isoformat(),
        "edited": bool(message.edited_at),
        "deleted": message.is_deleted,
        "pinned": message.is_pinned,
        "reactions": reactions,
        "mentions": message.mentions or [],
        "reply": reply,
    }


def emit_presence():
    ids = {item["user_id"] for item in ONLINE.values()}
    users = [user_public(db.session.get(User, uid)) for uid in ids if db.session.get(User, uid)]
    users.sort(key=lambda x: (x["status"] == "offline", x["display_name"].lower()))
    socketio.emit("presence", {"users": users})


def emit_room_refresh():
    rooms = db.session.scalars(select(Room).order_by(Room.is_default.desc(), Room.name.asc())).all()
    socketio.emit("rooms", {"rooms": [{"id": r.id, "name": r.name, "default": r.is_default, "created_by": r.created_by} for r in rooms]})


def find_or_create_dm(a, b):
    lo, hi = sorted((a, b))
    existing = db.session.scalar(select(DirectMessage).where(DirectMessage.user1_id == lo, DirectMessage.user2_id == hi))
    if existing:
        return existing
    dm = DirectMessage(user1_id=lo, user2_id=hi)
    db.session.add(dm)
    db.session.commit()
    return dm


def save_message(user, target_room, text, reply_to=None):
    settings = db.session.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    clean = filter_profanity(sanitize_text(text), settings.profanity_filter if settings else Config.PROFANITY_FILTER)
    mentions = []
    for username in MENTION_RE.findall(clean):
        mentioned = db.session.scalar(select(User).where(User.username.ilike(username)))
        if mentioned and mentioned.id != user.id:
            mentions.append(mentioned.id)
    message = Message(room=target_room, user_id=user.id, text=clean, mentions=sorted(set(mentions)), reply_to=reply_to if isinstance(reply_to, int) else None)
    db.session.add(message)
    db.session.commit()
    return message


def broadcast_message(message):
    socketio.emit("message", serialize_message(message), to=message.room)
    for uid in (message.mentions or []):
        socketio.emit("mention", {"message": serialize_message(message)}, room=f"user:{uid}")


def ensure_defaults(app):
    """Create starter rooms and the built-in bot after the DB is available."""
    with app.app_context():
        for name in ("general", "random", "tech"):
            if not db.session.scalar(select(Room).where(Room.name == name)):
                db.session.add(Room(name=name, created_by=None, is_default=True))
        bot = db.session.scalar(select(User).where(User.username == "ChatBot"))
        if not bot:
            bot = User(username="ChatBot", password_hash=generate_password_hash(secrets.token_urlsafe(32)), avatar="🤖", role="bot", status="offline")
            db.session.add(bot)
            db.session.flush()
            db.session.add(UserSettings(user_id=bot.id, display_name="ChatBot", profanity_filter=False))
        db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (ValueError, TypeError):
        return None


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Security extensions and login management.
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    socketio.init_app(app, cors_allowed_origins=None, manage_session=True, logger=False, engineio_logger=False)

    init_db_with_fallback(app, db, socketio)
    ensure_defaults(app)
    try:
        from db_fallback import USING_MEMORY, memory_reconnect_loop
        if USING_MEMORY:
            socketio.start_background_task(memory_reconnect_loop, app, db)
    except Exception as exc:
        app.logger.warning("Could not start database recovery monitor: %s", exc)

    @app.before_request
    def prepare_request():
        g.csp_nonce = secrets.token_urlsafe(18)

    @app.before_request
    def enforce_session_timeout():
        if current_user.is_authenticated:
            now = time.monotonic()
            last = session.get("last_activity_mono", now)
            if now - last > SESSION_TIMEOUT:
                logout_user()
                session.clear()
                return redirect(url_for("login", expired=1)) if request.endpoint not in {"login", "signup", "static", "health"} else None
            session["last_activity_mono"] = now
            session.permanent = True
            if request.endpoint not in {"health", "static"}:
                current_user.last_active = utcnow()
                current_user.status = "online"
                db.session.commit()

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src 'self' 'nonce-{getattr(g, 'csp_nonce', '')}' https://cdn.socket.io https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' ws: wss:; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        return render_template("error.html", title="Security check failed", message="Your security token expired or was invalid. Please refresh the page and try again."), 400

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", **database_status()})

    @app.route("/")
    def index():
        return redirect(url_for("chat" if current_user.is_authenticated else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("chat"))
        form = LoginForm()
        error = None
        if form.validate_on_submit():
            username = form.username.data.strip()
            user = db.session.scalar(select(User).where(User.username == username))
            if not user or user.banned or not check_password_hash(user.password_hash, form.password.data):
                error = "Invalid username or password"
            else:
                login_user(user, remember=False, duration=timedelta(minutes=30))
                session.permanent = True
                session["last_activity_mono"] = time.monotonic()
                user.last_active = utcnow()
                user.status = "online"
                db.session.commit()
                return redirect(url_for("chat"))
        if request.args.get("expired"):
            error = "Your session expired after 30 minutes of inactivity. Please log in again."
        return render_template("login.html", form=form, error=error)

    @app.route("/invite/<code>")
    def invite(code):
        # Invite links simply pre-fill signup; validation still happens server-side.
        return redirect(url_for("signup", invite=code.strip().upper()))

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if current_user.is_authenticated:
            return redirect(url_for("chat"))
        form = SignupForm()
        error = None
        if form.validate_on_submit():
            username = form.username.data.strip()
            error = validate_username(username) or validate_password(form.password.data)
            if not error and form.password.data != form.confirm_password.data:
                error = "Passwords do not match."
            invite = request.args.get("invite", "").strip().upper() or request.form.get("invite", "").strip().upper()
            if not error and not Config.OPEN_REGISTRATION:
                if not invite:
                    error = "Registration is invite-only. Use a valid invite link."
                else:
                    code = db.session.scalar(select(InviteCode).where(InviteCode.code == invite, InviteCode.used.is_(False)))
                    if not code:
                        error = "That invite code is invalid or already used."
            if not error:
                if db.session.scalar(select(User).where(User.username == username)):
                    error = "That username is already in use."
                else:
                    first_user = db.session.scalar(select(User.id).where(User.username != "ChatBot")) is None
                    user = User(username=username, password_hash=generate_password_hash(form.password.data), avatar=request.form.get("avatar") if request.form.get("avatar") in AVATARS else AVATARS[0], role="admin" if first_user else "member", status="offline")
                    db.session.add(user)
                    db.session.flush()
                    db.session.add(UserSettings(user_id=user.id, display_name=username))
                    if not Config.OPEN_REGISTRATION and invite:
                        code = db.session.scalar(select(InviteCode).where(InviteCode.code == invite, InviteCode.used.is_(False)))
                        code.used = True
                        code.used_by = user.id
                    db.session.commit()
                    return redirect(url_for("login", created=1))
        return render_template("signup.html", form=form, error=error, avatars=AVATARS, invite=request.args.get("invite", ""))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        current_user.status = "offline"
        db.session.commit()
        logout_user()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/chat")
    @login_required
    def chat():
        rooms = db.session.scalars(select(Room).order_by(Room.is_default.desc(), Room.name.asc())).all()
        users = db.session.scalars(select(User).where(User.banned.is_(False)).order_by(User.username.asc())).all()
        settings = db.session.scalar(select(UserSettings).where(UserSettings.user_id == current_user.id))
        return render_template("chat.html", rooms=rooms, users=users, current_user_public=user_public(current_user), settings=settings, avatars=AVATARS)

    @app.route("/api/messages")
    @login_required
    def api_messages():
        target = request.args.get("room", "room:1")
        before_id = request.args.get("before_id", type=int)
        if target.startswith("room:"):
            rid = int(target.split(":", 1)[1])
            room = db.session.get(Room, rid)
            if not valid_room_access(current_user, room):
                return jsonify({"error": "Room not found"}), 404
        else:
            partner = get_dm_partner(target, current_user.id)
            if not partner or not db.session.get(User, partner):
                return jsonify({"error": "Conversation not found"}), 404
        stmt = select(Message).where(Message.room == target).order_by(Message.id.desc()).limit(50)
        if before_id:
            stmt = select(Message).where(Message.room == target, Message.id < before_id).order_by(Message.id.desc()).limit(50)
        messages = list(db.session.scalars(stmt).all())
        messages.reverse()
        return jsonify({"messages": [serialize_message(m) for m in messages], "has_more": len(messages) == 50})

    @app.route("/api/rooms", methods=["POST"])
    @login_required
    def create_room():
        if not can_user_send(current_user):
            return jsonify({"error": "Your role cannot create rooms."}), 403
        name = sanitize_text(request.form.get("name", ""), 30)
        if not ROOM_RE.fullmatch(name):
            return jsonify({"error": "Room names must be 3-30 characters using letters, numbers, spaces, and hyphens."}), 400
        if db.session.scalar(select(Room).where(Room.name.ilike(name))):
            return jsonify({"error": "That room already exists."}), 409
        room = Room(name=name, created_by=current_user.id, is_default=False)
        db.session.add(room)
        db.session.commit()
        emit_room_refresh()
        return jsonify({"id": room.id, "name": room.name})

    @app.route("/api/rooms/<int:room_id>", methods=["DELETE"])
    @login_required
    def delete_room(room_id):
        room = db.session.get(Room, room_id)
        if not room or room.is_default or (room.created_by != current_user.id and current_user.role != "admin"):
            return jsonify({"error": "You cannot delete this room."}), 403
        db.session.delete(room)
        db.session.commit()
        emit_room_refresh()
        return jsonify({"ok": True})

    @app.route("/api/dm/<int:user_id>")
    @login_required
    def open_dm(user_id):
        if user_id == current_user.id:
            return jsonify({"error": "You cannot open a DM with yourself."}), 400
        other = db.session.get(User, user_id)
        if not other or other.banned:
            return jsonify({"error": "User not found."}), 404
        dm = find_or_create_dm(current_user.id, user_id)
        return jsonify({"room": dm_key(dm.user1_id, dm.user2_id), "user": user_public(other)})

    @app.route("/api/search")
    @login_required
    def search_messages():
        q = sanitize_text(request.args.get("q", ""), 80)
        room = request.args.get("room", "")
        if not q or not room:
            return jsonify({"results": []})
        if room.startswith("room:") and not db.session.get(Room, int(room.split(":", 1)[1])):
            return jsonify({"results": []})
        partner = get_dm_partner(room, current_user.id)
        if room.startswith("dm_") and not partner:
            return jsonify({"results": []})
        results = db.session.scalars(select(Message).where(Message.room == room, Message.text.ilike(f"%{q}%")).order_by(Message.id.desc()).limit(50)).all()
        return jsonify({"results": [serialize_message(m) for m in results]})

    @app.route("/api/calendar", methods=["GET", "POST"])
    @login_required
    def calendar_api():
        if request.method == "GET":
            start = request.args.get("start", type=int)
            end = request.args.get("end", type=int)
            stmt = select(CalendarEvent).order_by(CalendarEvent.starts_at.asc())
            if start is not None:
                stmt = stmt.where(CalendarEvent.starts_at >= datetime.fromtimestamp(start, tz=timezone.utc))
            if end is not None:
                stmt = stmt.where(CalendarEvent.starts_at <= datetime.fromtimestamp(end, tz=timezone.utc))
            events = db.session.scalars(stmt.limit(500)).all()
            return jsonify({"events": [serialize_event(e) for e in events]})
        if not can_user_send(current_user):
            return jsonify({"error": "Your role cannot create events."}), 403
        title = sanitize_text(request.form.get("title", ""), 120)
        desc = sanitize_text(request.form.get("description", ""), 1000)
        location = sanitize_text(request.form.get("location", ""), 180)
        try:
            starts = datetime.fromisoformat(request.form["starts_at"].replace("Z", "+00:00"))
            ends_raw = request.form.get("ends_at", "").strip()
            ends = datetime.fromisoformat(ends_raw.replace("Z", "+00:00")) if ends_raw else None
        except (KeyError, ValueError):
            return jsonify({"error": "Invalid event date/time."}), 400
        if not title or len(title) < 2 or (ends and ends <= starts):
            return jsonify({"error": "Enter a title and a valid time range."}), 400
        event = CalendarEvent(title=title, description=desc, location=location, starts_at=starts.astimezone(timezone.utc), ends_at=ends.astimezone(timezone.utc) if ends else None, created_by=current_user.id)
        db.session.add(event)
        db.session.commit()
        socketio.emit("calendar_event", {"action": "created", "event": serialize_event(event)})
        return jsonify(serialize_event(event)), 201

    @app.route("/api/calendar/<int:event_id>", methods=["DELETE"])
    @login_required
    def delete_event(event_id):
        event = db.session.get(CalendarEvent, event_id)
        if not event or (event.created_by != current_user.id and current_user.role != "admin"):
            return jsonify({"error": "You cannot delete this event."}), 403
        db.session.delete(event)
        db.session.commit()
        socketio.emit("calendar_event", {"action": "deleted", "id": event_id})
        return jsonify({"ok": True})

    @app.route("/api/settings", methods=["POST"])
    @login_required
    def settings_api():
        data = request.form
        settings = db.session.scalar(select(UserSettings).where(UserSettings.user_id == current_user.id))
        if not settings:
            settings = UserSettings(user_id=current_user.id)
            db.session.add(settings)
        name = sanitize_text(data.get("display_name", current_user.username), 30)
        if not 3 <= len(name) <= 30:
            return jsonify({"error": "Display name must be 3-30 characters."}), 400
        settings.display_name = name
        settings.theme = data.get("theme", "dark") if data.get("theme") in {"dark", "light", "system"} else "dark"
        settings.notify_mentions = data.get("notify_mentions") == "1"
        settings.notify_dms = data.get("notify_dms") == "1"
        settings.notify_room = data.get("notify_room") == "1"
        settings.profanity_filter = data.get("profanity_filter") == "1"
        if data.get("avatar") in AVATARS:
            current_user.avatar = data.get("avatar")
        db.session.commit()
        socketio.emit("profile_updated", {"user": user_public(current_user)})
        return jsonify({"ok": True, "user": user_public(current_user)})

    @app.route("/api/invites", methods=["POST"])
    @login_required
    @admin_required
    def create_invites():
        count = max(1, min(request.form.get("count", 1, type=int), 10))
        codes = []
        for _ in range(count):
            code = secrets.token_hex(4).upper()
            while db.session.scalar(select(InviteCode).where(InviteCode.code == code)):
                code = secrets.token_hex(4).upper()
            db.session.add(InviteCode(code=code, created_by=current_user.id))
            codes.append(code)
        db.session.commit()
        return jsonify({"codes": codes})

    @app.route("/api/admin/registration", methods=["POST"])
    @login_required
    @admin_required
    def registration_mode():
        value = request.form.get("open") == "1"
        # This is process-level configuration. It is deliberately not persisted because
        # the supplied deployment uses environment configuration as the source of truth.
        return jsonify({"open_registration": value, "note": "Set OPEN_REGISTRATION in Render for persistent configuration."})

    @app.route("/api/health/details")
    @login_required
    def health_details():
        return jsonify({"status": "ok", **database_status()})

    # ---------------- Socket.IO events ----------------
    @socketio.on("connect")
    def socket_connect(auth):
        if not current_user.is_authenticated or current_user.banned:
            return False
        if not socket_csrf_ok(auth):
            return False
        ONLINE[request.sid] = {"user_id": current_user.id, "username": current_user.username}
        join_room(f"user:{current_user.id}")
        current_user.last_active = utcnow()
        current_user.status = MANUAL_STATUS.get(current_user.id, "online")
        db.session.commit()
        emit_presence()

    @socketio.on("disconnect")
    def socket_disconnect():
        item = ONLINE.pop(request.sid, None)
        if item:
            # Only mark offline when the user has no other open tab/session.
            still_online = any(v["user_id"] == item["user_id"] for v in ONLINE.values())
            if not still_online:
                user = db.session.get(User, item["user_id"])
                if user:
                    user.status = "offline"
                    db.session.commit()
            emit_presence()

    @socketio.on("join_room")
    def socket_join(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        key = (data or {}).get("room", "")
        allowed = False
        if key.startswith("room:"):
            try:
                room = db.session.get(Room, int(key.split(":", 1)[1]))
                allowed = valid_room_access(current_user, room)
            except ValueError:
                pass
        elif get_dm_partner(key, current_user.id):
            allowed = db.session.get(User, get_dm_partner(key, current_user.id)) is not None
        if allowed:
            join_room(key)
            if key.startswith("dm_"):
                partner = get_dm_partner(key, current_user.id)
                receipt = db.session.scalar(select(UserReadReceipt).where(UserReadReceipt.owner_id == partner, UserReadReceipt.other_id == current_user.id))
                if not receipt:
                    receipt = UserReadReceipt(owner_id=partner, other_id=current_user.id, last_read_at=utcnow())
                    db.session.add(receipt)
                receipt.last_read_at = utcnow()
                db.session.commit()
                socketio.emit("dm_read", {"room": key, "user_id": current_user.id}, to=f"user:{partner}")

    @socketio.on("leave_room")
    def socket_leave(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        key = (data or {}).get("room", "")
        leave_room(key)
        TYPING.pop(key, None)

    @socketio.on("send_message")
    def socket_send(data):
        if not current_user.is_authenticated or current_user.banned or not socket_csrf_ok(data):
            return
        if not can_user_send(current_user):
            emit("error_message", {"message": "Your account is read-only."})
            return
        now = time.monotonic()
        if now - LAST_MESSAGE_AT.get(current_user.id, 0) < MESSAGE_INTERVAL:
            emit("error_message", {"message": "Slow down — please wait half a second between messages."})
            return
        text = (data or {}).get("text", "")
        if len(text) > MAX_MESSAGE_LENGTH:
            emit("error_message", {"message": "Messages are limited to 1000 characters."})
            return
        target = (data or {}).get("room", "")
        valid = False
        if target.startswith("room:"):
            try:
                valid = valid_room_access(current_user, db.session.get(Room, int(target.split(":", 1)[1])))
            except ValueError:
                pass
        else:
            partner = get_dm_partner(target, current_user.id)
            valid = bool(partner and db.session.get(User, partner))
        if not valid:
            emit("error_message", {"message": "That chat is unavailable."})
            return
        try:
            reply_to = int(data.get("reply_to")) if data.get("reply_to") else None
            if reply_to and not db.session.get(Message, reply_to):
                reply_to = None
            message = save_message(current_user, target, text, reply_to)
            LAST_MESSAGE_AT[current_user.id] = now
            current_user.last_active = utcnow()
            db.session.commit()
            broadcast_message(message)
        except Exception as exc:
            db.session.rollback()
            try:
                from db_fallback import switch_to_next_database
                switch_to_next_database(app, db)
            except Exception:
                app.logger.warning("Database recovery failed: %s", exc)
            emit("error_message", {"message": "Reconnecting... Please retry your message."})

        # Built-in bot replies are intentionally limited to deterministic commands.
        clean = sanitize_text(text).strip()

        if clean.lower().startswith("@chatbot") and target.startswith("room:"):
            bot = db.session.scalar(select(User).where(User.username == "ChatBot"))
            if bot:
                prompt = clean[len("@ChatBot"):].strip()
                emit("typing", {"user": user_public(bot), "room": target}, to=target, include_self=False)
                ai_reply = get_response(prompt)
                bot_msg = Message(room=target, user_id=bot.id, text=ai_reply, mentions=[])
                db.session.add(bot_msg)
                db.session.commit()
                broadcast_message(bot_msg)

        if clean.startswith("/") and target.startswith("room:"):
            bot_reply = bot_command(clean, target)
            if bot_reply:
                bot = db.session.scalar(select(User).where(User.username == "ChatBot"))
                if bot:
                    bot_msg = Message(room=target, user_id=bot.id, text=bot_reply, mentions=[])
                    db.session.add(bot_msg)
                    db.session.commit()
                    broadcast_message(bot_msg)

    @socketio.on("typing")
    def socket_typing(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        key = (data or {}).get("room", "")
        if not key:
            return
        now = time.monotonic()
        if now - LAST_TYPING_AT.get(current_user.id, 0) < TYPING_INTERVAL:
            return
        LAST_TYPING_AT[current_user.id] = now
        TYPING.setdefault(key, {})[current_user.id] = now
        emit("typing", {"user": user_public(current_user), "room": key}, to=key, include_self=False)

    @socketio.on("mark_read")
    def socket_mark_read(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        key = (data or {}).get("room", "")
        partner = get_dm_partner(key, current_user.id)
        if not partner:
            return
        receipt = db.session.scalar(select(UserReadReceipt).where(UserReadReceipt.owner_id == current_user.id, UserReadReceipt.other_id == partner))
        if not receipt:
            receipt = UserReadReceipt(owner_id=current_user.id, other_id=partner, last_read_at=utcnow())
            db.session.add(receipt)
        receipt.last_read_at = utcnow()
        db.session.commit()
        socketio.emit("dm_read", {"room": key, "user_id": current_user.id}, to=f"user:{partner}")

    @socketio.on("react")
    def socket_react(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        emoji = (data or {}).get("emoji")
        msg_id = (data or {}).get("message_id")
        if emoji not in ALLOWED_REACTIONS:
            return
        message = db.session.get(Message, msg_id)
        if not message or message.is_deleted:
            return
        room_ok = message.room.startswith("room:") or get_dm_partner(message.room, current_user.id)
        if not room_ok:
            return
        existing = db.session.scalar(select(Reaction).where(Reaction.message_id == msg_id, Reaction.user_id == current_user.id, Reaction.emoji == emoji))
        if existing:
            db.session.delete(existing)
        else:
            db.session.add(Reaction(message_id=msg_id, user_id=current_user.id, emoji=emoji))
        db.session.commit()
        socketio.emit("reaction_update", {"message": serialize_message(message)}, to=message.room)

    @socketio.on("edit_message")
    def socket_edit(data):
        if not current_user.is_authenticated or not can_user_send(current_user) or not socket_csrf_ok(data):
            return
        now = time.monotonic()
        if now - LAST_EDIT_AT.get(current_user.id, 0) < EDIT_INTERVAL:
            emit("error_message", {"message": "Slow down — edits are limited to once every 5 seconds."})
            return
        message = db.session.get(Message, data.get("message_id"))
        if not message or message.user_id != current_user.id or message.is_deleted:
            return
        text = sanitize_text(data.get("text", ""))
        if not text:
            emit("error_message", {"message": "Edited message cannot be empty."})
            return
        message.text = text
        message.edited_at = utcnow()
        db.session.commit()
        LAST_EDIT_AT[current_user.id] = now
        socketio.emit("message_updated", serialize_message(message), to=message.room)

    @socketio.on("delete_message")
    def socket_delete(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        message = db.session.get(Message, data.get("message_id"))
        if not message or (message.user_id != current_user.id and current_user.role != "admin"):
            return
        message.is_deleted = True
        message.text = "[message deleted]"
        db.session.commit()
        socketio.emit("message_updated", serialize_message(message), to=message.room)

    @socketio.on("pin_message")
    def socket_pin(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        message = db.session.get(Message, data.get("message_id"))
        if not message:
            return
        room_id = int(message.room.split(":", 1)[1]) if message.room.startswith("room:") else None
        room = db.session.get(Room, room_id) if room_id else None
        allowed = current_user.role == "admin" or (room and room.created_by == current_user.id)
        if not allowed:
            return
        if not message.is_pinned:
            pinned = db.session.scalars(select(Message).where(Message.room == message.room, Message.is_pinned.is_(True)).order_by(Message.pinned_at.asc())).all()
            if len(pinned) >= 3:
                pinned[0].is_pinned = False
                pinned[0].pinned_at = None
            message.is_pinned = True
            message.pinned_at = utcnow()
        else:
            message.is_pinned = False
            message.pinned_at = None
        db.session.commit()
        socketio.emit("message_updated", serialize_message(message), to=message.room)

    @socketio.on("set_status")
    def socket_status(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        value = (data or {}).get("status")
        if value not in {"online", "away", "dnd", "invisible"}:
            return
        MANUAL_STATUS[current_user.id] = value
        current_user.status = value
        current_user.last_active = utcnow()
        db.session.commit()
        emit_presence()

    @socketio.on("set_nickname")
    def socket_nickname(data):
        if not current_user.is_authenticated or not socket_csrf_ok(data):
            return
        room_id = data.get("room_id")
        nickname = sanitize_text(data.get("nickname", ""), 30)
        room = db.session.get(Room, room_id)
        if not room or not 3 <= len(nickname) <= 30:
            return
        row = db.session.scalar(select(RoomNickname).where(RoomNickname.user_id == current_user.id, RoomNickname.room_id == room.id))
        if not row:
            row = RoomNickname(user_id=current_user.id, room_id=room.id, nickname=nickname)
            db.session.add(row)
        else:
            row.nickname = nickname
        db.session.commit()
        emit("nickname_saved", {"room_id": room_id, "nickname": nickname})

    return app


def serialize_event(event):
    creator = db.session.get(User, event.created_by) if event.created_by else None
    return {"id": event.id, "title": event.title, "description": event.description, "starts_at": normalize_datetime(event.starts_at).isoformat(), "ends_at": normalize_datetime(event.ends_at).isoformat() if event.ends_at else None, "location": event.location, "created_by": creator.username if creator else "Deleted user"}


def bot_command(command, room):
    """Return deterministic built-in bot content for the requested commands."""
    if command == "/help":
        return "Commands: /help /ping /roll /flip /8ball /joke /time /users /quote"
    if command == "/ping":
        return "Pong! 🏓"
    if command == "/roll":
        return f"You rolled {random.randint(1, 100)}."
    if command == "/flip":
        return random.choice(["Heads", "Tails"])
    if command == "/time":
        return f"Server time: {utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    if command == "/users":
        names = sorted({x["username"] for x in ONLINE.values()})
        return "Online: " + (", ".join(names) if names else "nobody")
    if command == "/8ball":
        return random.choice([
            "It is certain.", "Without a doubt.", "Yes — definitely.", "You may rely on it.", "Most likely.", "Outlook good.", "Signs point to yes.", "Reply hazy — try again.", "Ask again later.", "Better not tell you now.", "Cannot predict that yet.", "Concentrate and ask again.", "Don't count on it.", "My reply is no.", "Outlook not so good.", "Very doubtful.", "Probably not.", "Not today.", "The odds are changing.", "There is a chance."
        ])
    if command == "/joke":
        return random.choice([
            "Why do programmers prefer dark mode? Because light attracts bugs.",
            "A SQL query walks into a bar, walks up to two tables, and asks: Can I join you?",
            "There are only 10 kinds of people: those who understand binary and those who don't.",
            "Why did the developer go broke? Because they used up all their cache.",
            "I would tell you a UDP joke, but you might not get it.",
            "Why did the function return early? It had a deadline.",
            "A byte walks into a bar and says, 'Can I have a bit?'",
            "Why was the computer cold? It left its Windows open.",
            "Debugging: being the detective in a crime movie where you are also the criminal.",
            "The programmer's coffee was exceptional. It had great Java behavior.",
        ])
    if command == "/quote":
        return random.choice([
            "Small progress is still progress.", "Make it work, then make it clear.", "Curiosity is a powerful debugging tool.", "Build, test, learn, repeat.", "A good question can unlock a hard problem.", "Consistency beats a burst of effort.", "Every bug is information.", "Ideas become useful when you build them.", "Keep experiments small and measurable.", "Learn the rule, then understand why it works.", "Good systems fail safely.", "Clarity is a feature.", "Reliable beats flashy.", "Start simple and expand deliberately.", "Your future self will thank you for comments.", "Testing is part of building.", "Make failure recoverable.", "Readable code is easier to improve.", "Good tools amplify good thinking.", "Finish small things before making big things."
        ])
    return None


app = create_app()


if __name__ == "__main__":
    print(f"C.S.P. Chat starting on http://127.0.0.1:{Config.PORT}")
    socketio.run(app, host="0.0.0.0", port=Config.PORT, debug=False)

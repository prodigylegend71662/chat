"""Database models for C.S.P. Chat."""
from datetime import datetime, timezone

from extensions import db


def utcnow():
    return datetime.now(timezone.utc)


from flask_login import UserMixin

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    avatar = db.Column(db.String(8), nullable=True)
    role = db.Column(db.String(12), nullable=False, default="member")
    status = db.Column(db.String(16), nullable=False, default="offline")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_active = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    banned = db.Column(db.Boolean, nullable=False, default=False)


class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False)
    display_name = db.Column(db.String(30), nullable=True)
    theme = db.Column(db.String(10), nullable=False, default="dark")
    notify_mentions = db.Column(db.Boolean, nullable=False, default=True)
    notify_dms = db.Column(db.Boolean, nullable=False, default=True)
    notify_room = db.Column(db.Boolean, nullable=False, default=True)
    profanity_filter = db.Column(db.Boolean, nullable=False, default=True)


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), unique=True, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    is_default = db.Column(db.Boolean, nullable=False, default=False)


class DirectMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user1_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    user2_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(80), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    edited_at = db.Column(db.DateTime(timezone=True), nullable=True)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    pinned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reply_to = db.Column(db.Integer, db.ForeignKey("message.id", ondelete="SET NULL"), nullable=True)
    mentions = db.Column(db.JSON, nullable=False, default=list)


class Reaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    emoji = db.Column(db.String(8), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (db.UniqueConstraint("message_id", "user_id", "emoji", name="uq_reaction"),)


class RoomNickname(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id", ondelete="CASCADE"), nullable=False)
    nickname = db.Column(db.String(30), nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "room_id", name="uq_room_nickname"),)


class ScheduledMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id", ondelete="CASCADE"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    send_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    sent = db.Column(db.Boolean, nullable=False, default=False)


class MessageAttachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id", ondelete="CASCADE"), nullable=False)
    url = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(20), nullable=False)


class InviteCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    used = db.Column(db.Boolean, nullable=False, default=False)
    used_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)


class CalendarEvent(db.Model):
    """Global calendar event visible to all logged-in users."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=True)
    location = db.Column(db.String(180), nullable=False, default="")
    created_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class UserReadReceipt(db.Model):
    """Last-read time for a user's DM conversation with another user."""
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    other_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    last_read_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (db.UniqueConstraint("owner_id", "other_id", name="uq_read_receipt"),)

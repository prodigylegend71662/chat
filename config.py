"""Environment-driven configuration for C.S.P. Chat."""
import os
import secrets
import warnings


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY:
        SECRET_KEY = secrets.token_hex(32)
        warnings.warn("SECRET_KEY is not set. A temporary key was generated; sessions reset on restart.")

    # Render supplies PORT. The development default makes local startup easy.
    PORT = int(os.getenv("PORT", "10000"))

    # Database fallback chain.
    DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
    SUPABASE_DATABASE_URL = os.getenv("SUPABASE_DATABASE_URL", "").strip()

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Security settings.
    SESSION_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true" if DATABASE_URL else "false").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 7 * 24 * 60 * 60
    WTF_CSRF_TIME_LIMIT = 7 * 24 * 60 * 60

    OPEN_REGISTRATION = os.getenv("OPEN_REGISTRATION", "true").lower() == "true"
    PROFANITY_FILTER = os.getenv("PROFANITY_FILTER", "true").lower() == "true"
    IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")

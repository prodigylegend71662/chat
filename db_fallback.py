"""Database selection and controlled recovery for C.S.P.

The startup chain is Render PostgreSQL -> Supabase PostgreSQL -> local SQLite ->
in-memory SQLite. SQLAlchemy ORM remains the only persistence API.
"""
from sqlalchemy import create_engine, select, literal
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

USING_MEMORY = False
ACTIVE_DATABASE = "unknown"
ACTIVE_URL = ""


def _engine_options(url):
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite:///:memory:"):
        options.update({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool})
    elif url.startswith("sqlite://"):
        options["connect_args"] = {"check_same_thread": False}
    return options


def _normalize(url):
    """Make Render's postgres:// URL acceptable to modern SQLAlchemy."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _test(url):
    engine = create_engine(_normalize(url), **_engine_options(_normalize(url)))
    with engine.connect() as conn:
        conn.execute(select(literal(1)))
    engine.dispose()


def init_db_with_fallback(app, db, socketio):
    global USING_MEMORY, ACTIVE_DATABASE, ACTIVE_URL
    candidates = []
    if app.config.get("DATABASE_URL"):
        candidates.append(("render_postgres", app.config["DATABASE_URL"]))
    if app.config.get("SUPABASE_DATABASE_URL"):
        candidates.append(("supabase_postgres", app.config["SUPABASE_DATABASE_URL"]))
    candidates.append(("sqlite_local", "sqlite:///chat.db"))
    candidates.append(("in_memory", "sqlite:///:memory:"))

    for name, url in candidates:
        try:
            normalized = _normalize(url)
            _test(normalized)
            app.config["SQLALCHEMY_DATABASE_URI"] = normalized
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_options(normalized)
            # db.Model is already imported through the application models, so testing
            # create_all on this candidate verifies both connectivity and permissions.
            test_engine = create_engine(normalized, **_engine_options(normalized))
            db.Model.metadata.create_all(bind=test_engine)
            test_engine.dispose()
            db.init_app(app)
            with app.app_context():
                db.create_all()
            ACTIVE_DATABASE = name
            ACTIVE_URL = normalized
            USING_MEMORY = name == "in_memory"
            if USING_MEMORY:
                app.logger.warning("WARNING: Using in-memory storage. All data will be lost on restart.")
            return name
        except (OperationalError, Exception) as exc:  # Startup must survive a failed primary database.
            app.logger.warning("Database candidate %s failed: %s", name, exc)

    raise RuntimeError("No database backend could be initialized")


def database_status():
    return {
        "database": ACTIVE_DATABASE,
        "fallback_active": ACTIVE_DATABASE not in {"render_postgres", "unknown"},
    }


def _candidate_list(app):
    candidates = []
    if app.config.get("DATABASE_URL"):
        candidates.append(("render_postgres", _normalize(app.config["DATABASE_URL"])))
    if app.config.get("SUPABASE_DATABASE_URL"):
        candidates.append(("supabase_postgres", _normalize(app.config["SUPABASE_DATABASE_URL"])))
    candidates.append(("sqlite_local", "sqlite:///chat.db"))
    candidates.append(("in_memory", "sqlite:///:memory:"))
    return candidates


def switch_to_next_database(app, db):
    """Swap the Flask-SQLAlchemy default engine to the first working backend."""
    global ACTIVE_DATABASE, ACTIVE_URL, USING_MEMORY
    candidates = _candidate_list(app)
    current_index = -1 if ACTIVE_DATABASE == "in_memory" else next((i for i, item in enumerate(candidates) if item[0] == ACTIVE_DATABASE), -1)
    for name, url in candidates[current_index + 1:]:
        try:
            _test(url)
            new_engine = create_engine(url, **_engine_options(url))
            with new_engine.begin() as connection:
                db.Model.metadata.create_all(bind=connection)
            with app.app_context():
                db.engines[None].dispose()
                db.engines[None] = new_engine
            ACTIVE_DATABASE = name
            ACTIVE_URL = url
            USING_MEMORY = name == "in_memory"
            if USING_MEMORY:
                app.logger.warning("WARNING: Using in-memory storage. All data will be lost on restart.")
            else:
                app.logger.warning("Reconnected to %s.", name)
            return name
        except (OperationalError, Exception) as exc:
            app.logger.warning("Recovery candidate %s failed: %s", name, exc)
    return ACTIVE_DATABASE


def memory_reconnect_loop(app, db):
    """Try the durable backends periodically while temporary memory storage is active."""
    import time

    while True:
        time.sleep(300)
        if not USING_MEMORY:
            continue
        try:
            with app.app_context():
                switch_to_next_database(app, db)
        except (OperationalError, Exception) as exc:
            app.logger.warning("Periodic database recovery failed: %s", exc)

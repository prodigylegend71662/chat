"""Flask extensions are created here so the app factory can initialize them."""
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect


db = SQLAlchemy()
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading", ping_timeout=60, ping_interval=25)
login_manager = LoginManager()
csrf = CSRFProtect()

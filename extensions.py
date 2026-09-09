"""Flask extensions are created here so the app factory can initialize them."""
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect


db = SQLAlchemy()
socketio = SocketIO(cors_allowed_origins=[], async_mode="eventlet")
login_manager = LoginManager()
csrf = CSRFProtect()

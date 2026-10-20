"""Login, sessions and role-based access control."""
import time
from functools import wraps

from flask import g, jsonify, session
from werkzeug.security import check_password_hash

from . import db

# Failed login attempts per username: (count, time of the latest failure). Reset on success
# or after LOCKOUT_SECONDS, so a guessing attacker cannot lock an account (even admin) forever.
# In memory, per server process.
_failed = {}
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60


def authenticate(username, password):
    """Return (user, error_message)."""
    username = (username or "").strip().lower()
    count, last = _failed.get(username, (0, 0.0))
    if count >= MAX_FAILED_LOGINS:
        if time.monotonic() - last < LOCKOUT_SECONDS:
            return None, ("Too many failed attempts. Try again in 15 minutes, "
                          "or ask an administrator to reset your password.")
        count = 0  # lockout expired
    row = db.get_user_by_username(username)
    if not row or not check_password_hash(row["password_hash"], password or ""):
        _failed[username] = (count + 1, time.monotonic())
        return None, "Username or password is incorrect."
    if not row["active"]:
        return None, "This account is deactivated. Contact an administrator."
    _failed.pop(username, None)
    return db.user_to_dict(row), None


def reset_failed_logins(username):
    _failed.pop((username or "").lower(), None)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = db.user_to_dict(db.get_user(user_id))
    return user if user and user["active"] else None


def require_roles(*roles):
    """Allow the request only for signed-in users with one of the given roles.
    With no roles listed, any signed-in user is allowed."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify(error="Sign in to continue."), 401
            if roles and user["role"] not in roles:
                return jsonify(error="Your role does not have access to this action."), 403
            g.user = user
            return view(*args, **kwargs)
        return wrapper
    return decorator

"""Login, sessions and role-based access control."""
from functools import wraps

from flask import g, jsonify, session
from werkzeug.security import check_password_hash

from . import db

# Failed login attempts per username; reset on success (in memory, per server process).
_failed = {}
MAX_FAILED_LOGINS = 5


def authenticate(username, password):
    """Return (user, error_message)."""
    username = (username or "").strip().lower()
    if _failed.get(username, 0) >= MAX_FAILED_LOGINS:
        return None, "Too many failed attempts. Ask an administrator to reset your password."
    row = db.get_user_by_username(username)
    if not row or not check_password_hash(row["password_hash"], password or ""):
        _failed[username] = _failed.get(username, 0) + 1
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

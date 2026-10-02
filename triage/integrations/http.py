"""Shared HTTP helper for tracker connectors: timeouts, safe error messages, HMAC checks."""
import hashlib
import hmac

import requests

from .base import SyncError


def call(method, url, *, service, timeout=15, **kwargs):
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.Timeout:
        raise SyncError(f"{service} did not respond in time")
    except requests.RequestException as exc:
        raise SyncError(f"{service} unreachable ({type(exc).__name__})")
    if resp.status_code in (401, 403):
        raise SyncError(f"{service} rejected the credentials or permissions (HTTP {resp.status_code})")
    if resp.status_code == 404:
        raise SyncError(f"{service}: item not found (HTTP 404)")
    if resp.status_code == 429:
        raise SyncError(f"{service} rate limit reached; will retry later")
    if resp.status_code >= 400:
        raise SyncError(f"{service} error (HTTP {resp.status_code})")
    return resp.json() if resp.content else None


def valid_signature(secret, raw_body, header_value):
    """Constant-time check of a 'sha256=<hex>' HMAC header over the raw request body."""
    if not secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value)

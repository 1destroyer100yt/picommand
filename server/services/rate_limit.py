"""
Rate limiting (Issue #7)

A single shared slowapi Limiter, keyed by client IP. Imported by routes.py
(for the @limiter.limit decorator on login) and by main.py (to register the
limiter and the 429 handler on the app).
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

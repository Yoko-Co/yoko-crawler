"""
Bearer token authentication dependency for FastAPI.

Uses secrets.compare_digest for constant-time comparison.
Applied via separate routers — authenticated for /crawl, public for /health.
"""

from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer(auto_error=False)


def verify_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> None:
    """Verify the Bearer token against YOKO_CRAWL_API_KEY environment variable."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    api_key = os.environ.get("YOKO_CRAWL_API_KEY", "")
    if not secrets.compare_digest(credentials.credentials, api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

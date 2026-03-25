"""Tests for auth.py."""

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from auth import verify_api_key


class TestVerifyApiKey:
    def test_valid_key(self):
        key = "a" * 48
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
        with patch.dict(os.environ, {"YOKO_CRAWL_API_KEY": key}):
            # Should not raise.
            verify_api_key(creds)

    def test_invalid_key(self):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="wrong-key"
        )
        with patch.dict(os.environ, {"YOKO_CRAWL_API_KEY": "a" * 48}):
            with pytest.raises(HTTPException) as exc_info:
                verify_api_key(creds)
            assert exc_info.value.status_code == 401

    def test_empty_env_var(self):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="anything"
        )
        with patch.dict(os.environ, {"YOKO_CRAWL_API_KEY": ""}):
            with pytest.raises(HTTPException) as exc_info:
                verify_api_key(creds)
            assert exc_info.value.status_code == 401

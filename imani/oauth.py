"""Google OAuth 2.0 / OpenID Connect sign-in.

Implemented with the standard library only (``urllib``) so the app keeps its
"no extra dependencies" property. The flow uses Authorization Code + PKCE and
retrieves the profile from Google's UserInfo endpoint over TLS.

This module performs network I/O; it is deliberately free of Flask specifics so
it can be unit-tested by monkeypatching the small HTTP helpers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPE = "openid email profile"
HTTP_TIMEOUT = 10


class OAuthError(Exception):
    """Raised when the OAuth exchange fails."""


def is_configured(config) -> bool:
    return bool(config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"))


def new_state() -> str:
    """A random, single-use anti-CSRF value for the authorization request."""
    return secrets.token_urlsafe(32)


def pkce_pair() -> tuple[str, str]:
    """Return an (code_verifier, code_challenge) pair using S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_uri: str, state: str,
                        code_challenge: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"


# --- HTTP helpers (monkeypatched in tests) --------------------------------

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise OAuthError(f"Token endpoint returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OAuthError("Could not reach Google's token endpoint.") from exc


def _get_json(url: str, bearer: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"UserInfo endpoint returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OAuthError("Could not reach Google's UserInfo endpoint.") from exc


def exchange_code(code: str, client_id: str, client_secret: str,
                  redirect_uri: str, code_verifier: str) -> dict:
    """Swap an authorization code for tokens."""
    return _post_form(TOKEN_ENDPOINT, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    })


def fetch_userinfo(access_token: str) -> dict:
    """Return Google's OpenID Connect profile claims for the access token."""
    return _get_json(USERINFO_ENDPOINT, access_token)


def domain_allowed(email: str, allowed_domains: str) -> bool:
    """If ``allowed_domains`` is set, the email must belong to one of them."""
    allowed = [d.strip().lower() for d in (allowed_domains or "").split(",") if d.strip()]
    if not allowed:
        return True
    domain = (email or "").rsplit("@", 1)[-1].lower()
    return domain in allowed

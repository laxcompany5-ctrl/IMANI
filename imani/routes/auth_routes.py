"""Login, logout and sign-up routes."""
from __future__ import annotations

import hmac

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .. import auth, oauth
from ..models import AuditRepository, UserRepository
from ..validation import password_problems, valid_email, valid_username

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Stored as the password hash for accounts that only sign in with Google.
NO_PASSWORD = "!oauth-no-password"

# Session keys used during the Google handshake.
OAUTH_STATE_KEY = "_oauth_state"
OAUTH_VERIFIER_KEY = "_oauth_verifier"
OAUTH_NEXT_KEY = "_oauth_next"

# Messages shown when authentication fails, keyed by reason.
FAILURE_MESSAGES = {
    auth.LOCKED: "This account is temporarily locked after too many failed "
                 "attempts. Please try again in a few minutes.",
    auth.PENDING: "Your account is awaiting administrator approval. You will be "
                  "able to sign in once it has been approved.",
    auth.REJECTED: "This account request was not approved. Please contact an "
                   "administrator.",
    auth.DISABLED: "This account has been disabled. Please contact an "
                   "administrator.",
    auth.INVALID: "Invalid username/email or password.",
}

REQUESTABLE_ROLES = ("analyst", "viewer")


@bp.route("/login", methods=("GET", "POST"))
def login():
    # Already signed in? Go straight to the dashboard.
    if auth.current_user() is not None:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user, reason = auth.authenticate(identifier, password)

        if user is None:
            AuditRepository.record(
                "LOGIN_FAILED", "auth", None,
                f"failed sign-in for '{identifier}' ({reason})",
                identifier or "unknown",
            )
            flash(FAILURE_MESSAGES.get(reason, FAILURE_MESSAGES[auth.INVALID]), "danger")
        else:
            auth.login(user)
            AuditRepository.record(
                "LOGIN", "auth", user["id"], None, user["username"]
            )
            flash(f"Welcome back, {user['full_name']}.", "success")
            nxt = request.args.get("next") or url_for("dashboard.index")
            if not nxt.startswith("/") or nxt.startswith("//"):  # block open-redirects
                nxt = url_for("dashboard.index")
            return redirect(nxt)

    return render_template("auth/login.html")


@bp.route("/signup", methods=("GET", "POST"))
def signup():
    if auth.current_user() is not None:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        requested_role = request.form.get("requested_role", "analyst").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors: dict[str, str] = {}
        if not full_name:
            errors["full_name"] = "Full name is required."
        if not valid_username(username):
            errors["username"] = "Use 3–32 characters: letters, numbers, . _ -"
        elif UserRepository.get_by_username(username):
            errors["username"] = "That username is already taken."
        if not valid_email(email):
            errors["email"] = "Enter a valid email address."
        elif UserRepository.get_by_email(email):
            errors["email"] = "That email is already registered."
        if requested_role not in REQUESTABLE_ROLES:
            requested_role = "analyst"

        problems = password_problems(password, confirm)
        if problems:
            errors["password"] = "Password must " + ", ".join(problems) + "."

        if errors:
            for message in errors.values():
                flash(message, "danger")
            return render_template(
                "auth/signup.html",
                roles=REQUESTABLE_ROLES,
                form={"full_name": full_name, "username": username,
                      "email": email, "requested_role": requested_role},
            ), 400

        UserRepository.create(
            username=username,
            email=email,
            full_name=full_name,
            password_hash=auth.hash_password(password),
            role="viewer",              # least privilege until approved
            status="pending",
            requested_role=requested_role,
        )
        AuditRepository.record(
            "SIGNUP", "users", None,
            f"new sign-up request '{username}' requesting '{requested_role}'",
            username,
        )
        flash(
            "Your request has been submitted. An administrator will review it "
            "before you can sign in.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template(
        "auth/signup.html", roles=REQUESTABLE_ROLES,
        form={"requested_role": "analyst"},
    )


@bp.route("/logout", methods=("POST",))
def logout():
    user = auth.current_user()
    if user:
        AuditRepository.record("LOGOUT", "auth", user["id"], None, user["username"])
    auth.logout()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


# --- Google sign-in -------------------------------------------------------

def _google_redirect_uri() -> str:
    override = current_app.config.get("GOOGLE_REDIRECT_URI")
    return override or url_for("auth.google_callback", _external=True)


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("dashboard.index")


@bp.route("/google")
def google_login():
    """Start the Google OAuth handshake."""
    if auth.current_user() is not None:
        return redirect(url_for("dashboard.index"))

    if not oauth.is_configured(current_app.config):
        flash(
            "Google sign-in is not configured yet. An administrator must add "
            "the Google client ID and secret.",
            "warning",
        )
        return redirect(url_for("auth.login"))

    state = oauth.new_state()
    verifier, challenge = oauth.pkce_pair()
    session[OAUTH_STATE_KEY] = state
    session[OAUTH_VERIFIER_KEY] = verifier
    session[OAUTH_NEXT_KEY] = request.args.get("next", "")

    url = oauth.build_authorize_url(
        current_app.config["GOOGLE_CLIENT_ID"],
        _google_redirect_uri(),
        state,
        challenge,
    )
    return redirect(url)


@bp.route("/google/callback")
def google_callback():
    """Handle Google's redirect back to IMANI."""
    if auth.current_user() is not None:
        return redirect(url_for("dashboard.index"))

    if not oauth.is_configured(current_app.config):
        flash("Google sign-in is not configured.", "warning")
        return redirect(url_for("auth.login"))

    if request.args.get("error"):
        flash("Google sign-in was cancelled.", "warning")
        return redirect(url_for("auth.login"))

    expected_state = session.pop(OAUTH_STATE_KEY, None)
    verifier = session.pop(OAUTH_VERIFIER_KEY, None)
    nxt = _safe_next(session.pop(OAUTH_NEXT_KEY, None))

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not code or not expected_state or not hmac.compare_digest(state, expected_state):
        AuditRepository.record("LOGIN_FAILED", "auth", None,
                               "google callback failed state check", "google")
        flash("Your sign-in session expired. Please try again.", "danger")
        return redirect(url_for("auth.login"))

    try:
        tokens = oauth.exchange_code(
            code,
            current_app.config["GOOGLE_CLIENT_ID"],
            current_app.config["GOOGLE_CLIENT_SECRET"],
            _google_redirect_uri(),
            verifier,
        )
        info = oauth.fetch_userinfo(tokens["access_token"])
    except (oauth.OAuthError, KeyError) as exc:
        AuditRepository.record("LOGIN_FAILED", "auth", None,
                               f"google oauth error: {exc}", "google")
        flash("We couldn't complete Google sign-in. Please try again.", "danger")
        return redirect(url_for("auth.login"))

    return _complete_google_login(info, nxt)


def _complete_google_login(info: dict, nxt: str):
    """Map Google profile claims to an IMANI account and sign the user in."""
    google_id = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    verified = info.get("email_verified")
    name = (info.get("name") or "").strip() or email.split("@")[0]
    picture = info.get("picture")

    # We only trust Google-verified email addresses.
    if not google_id or not email or verified is not True:
        AuditRepository.record("LOGIN_FAILED", "auth", None,
                               "google account without a verified email", "google")
        flash("Your Google account did not provide a verified email address.",
              "danger")
        return redirect(url_for("auth.login"))

    if not oauth.domain_allowed(email, current_app.config.get("GOOGLE_ALLOWED_DOMAINS", "")):
        AuditRepository.record("LOGIN_FAILED", "auth", None,
                               f"google email domain not allowed: {email}", "google")
        flash("Sign-in with that email domain is not permitted.", "danger")
        return redirect(url_for("auth.login"))

    user = UserRepository.get_by_google_id(google_id)

    if user is None:
        user = UserRepository.get_by_email(email)
        if user is not None:
            # Link the Google identity to the existing account.
            UserRepository.link_google(user["id"], google_id, picture)
            user = UserRepository.get(user["id"])
        else:
            # Brand-new person: create a pending access request.
            username = UserRepository.suggest_username(email.split("@")[0])
            new_id = UserRepository.create(
                username=username,
                email=email,
                full_name=name,
                password_hash=NO_PASSWORD,
                role="viewer",
                status="pending",
                requested_role="analyst",
                google_id=google_id,
                avatar_url=picture,
                auth_provider="google",
            )
            AuditRepository.record("SIGNUP", "users", new_id,
                                   f"google sign-up '{username}'", username)
            flash(
                "Your Google account was verified. An administrator will review "
                "your access request before you can sign in.",
                "success",
            )
            return redirect(url_for("auth.login"))

    status = user["status"]
    if status == "pending":
        flash("Your account is awaiting administrator approval.", "warning")
        return redirect(url_for("auth.login"))
    if status == "rejected":
        flash("This account request was not approved.", "danger")
        return redirect(url_for("auth.login"))
    if status != "active":
        flash("This account has been disabled.", "danger")
        return redirect(url_for("auth.login"))

    auth.login(user)
    AuditRepository.record("LOGIN", "auth", user["id"], "signed in with Google",
                           user["username"])
    flash(f"Welcome back, {user['full_name']}.", "success")
    return redirect(nxt)

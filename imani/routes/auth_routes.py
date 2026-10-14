"""Login, logout and sign-up routes."""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import auth
from ..models import AuditRepository, UserRepository
from ..validation import password_problems, valid_email, valid_username

bp = Blueprint("auth", __name__, url_prefix="/auth")

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

"""Administrator tools: account approvals and user management."""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import auth
from ..models import AuditRepository, UserRepository

bp = Blueprint("admin", __name__, url_prefix="/admin")

ASSIGNABLE_ROLES = ("admin", "analyst", "viewer")


def _pending_or_404(user_id: int):
    user = UserRepository.get(user_id)
    if user is None or user["status"] != "pending":
        abort(404)
    return user


@bp.route("/approvals")
@auth.role_required("admin")
def approvals():
    return render_template(
        "admin/approvals.html",
        pending=UserRepository.by_status("pending"),
    )


@bp.route("/approvals/<int:user_id>/approve", methods=("POST",))
@auth.role_required("admin")
def approve(user_id: int):
    user = _pending_or_404(user_id)
    role = request.form.get("role", "analyst").strip().lower()
    if role not in ASSIGNABLE_ROLES:
        flash("Choose a valid role.", "danger")
        return redirect(url_for("admin.approvals"))

    UserRepository.decide(user_id, "active", role, auth.current_user()["id"])
    AuditRepository.record(
        "APPROVE", "users", user_id,
        f"approved '{user['username']}' as {role}",
        auth.current_user()["username"],
    )
    flash(f"Approved '{user['username']}' as {role}.", "success")
    return redirect(url_for("admin.approvals"))


@bp.route("/approvals/<int:user_id>/reject", methods=("POST",))
@auth.role_required("admin")
def reject(user_id: int):
    user = _pending_or_404(user_id)
    UserRepository.decide(user_id, "rejected", None, auth.current_user()["id"])
    AuditRepository.record(
        "REJECT", "users", user_id,
        f"rejected sign-up '{user['username']}'",
        auth.current_user()["username"],
    )
    flash(f"Rejected '{user['username']}'.", "success")
    return redirect(url_for("admin.approvals"))


@bp.route("/users")
@auth.role_required("admin")
def users():
    return render_template("admin/users.html", users=UserRepository.all())


@bp.route("/users/<int:user_id>/toggle", methods=("POST",))
@auth.role_required("admin")
def toggle(user_id: int):
    user = UserRepository.get(user_id)
    if user is None:
        abort(404)
    if user["id"] == auth.current_user()["id"]:
        flash("You cannot change the status of your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user["status"] not in ("active", "disabled"):
        flash("Only active or disabled accounts can be toggled.", "warning")
        return redirect(url_for("admin.users"))

    new_active = user["status"] != "active"
    UserRepository.set_active(user_id, new_active)
    AuditRepository.record(
        "UPDATE", "users", user_id,
        f"{'activated' if new_active else 'disabled'} '{user['username']}'",
        auth.current_user()["username"],
    )
    flash(
        f"'{user['username']}' {'activated' if new_active else 'disabled'}.",
        "success",
    )
    return redirect(url_for("admin.users"))

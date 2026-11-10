"""Camera groups / zones (Bit 12)."""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import auth
from ..models import AuditRepository, CameraGroupRepository

bp = Blueprint("camera_groups", __name__, url_prefix="/camera-groups")

MUTATION_ROLES = ("analyst", "admin")


def _get_or_404(group_id: int):
    group = CameraGroupRepository.get(group_id)
    if group is None:
        abort(404)
    return group


@bp.route("/")
@auth.login_required
def index():
    groups = CameraGroupRepository.all()
    decorated = []
    for g in groups:
        cameras = CameraGroupRepository.cameras(g["id"])
        decorated.append(dict(g, cameras=cameras, count=len(cameras)))
    return render_template("camera_groups/index.html", groups=decorated,
                           can_manage=auth.current_user()["role"] in MUTATION_ROLES)


@bp.route("/new", methods=("GET", "POST"))
@auth.role_required("analyst")
def create():
    values = {"name": "", "description": ""}
    if request.method == "POST":
        values = {"name": request.form.get("name", "").strip(),
                  "description": request.form.get("description", "").strip()}
        if not values["name"]:
            flash("Give the group a name.", "danger")
            return render_template("camera_groups/form.html", values=values), 400
        clash = CameraGroupRepository.get_by_name(values["name"])
        if clash:
            flash("A group with that name already exists.", "danger")
            return render_template("camera_groups/form.html", values=values), 400
        gid = CameraGroupRepository.create(values)
        AuditRepository.record("INSERT", "camera_groups", gid,
                               f"created camera group '{values['name']}'",
                               auth.current_user()["username"])
        flash(f"Camera group '{values['name']}' created.", "success")
        return redirect(url_for("camera_groups.index"))
    return render_template("camera_groups/form.html", values=values)


@bp.route("/<int:group_id>/edit", methods=("GET", "POST"))
@auth.role_required("analyst")
def edit(group_id: int):
    group = _get_or_404(group_id)
    values = {"name": group["name"], "description": group["description"] or ""}
    if request.method == "POST":
        values = {"name": request.form.get("name", "").strip(),
                  "description": request.form.get("description", "").strip()}
        if not values["name"]:
            flash("Give the group a name.", "danger")
            return render_template("camera_groups/form.html", values=values, group_id=group_id), 400
        clash = CameraGroupRepository.get_by_name(values["name"])
        if clash and clash["id"] != group_id:
            flash("A group with that name already exists.", "danger")
            return render_template("camera_groups/form.html", values=values, group_id=group_id), 400
        CameraGroupRepository.update(group_id, values)
        AuditRepository.record("UPDATE", "camera_groups", group_id,
                               f"updated camera group '{values['name']}'",
                               auth.current_user()["username"])
        flash("Camera group updated.", "success")
        return redirect(url_for("camera_groups.index"))
    return render_template("camera_groups/form.html", values=values, group_id=group_id)


@bp.route("/<int:group_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(group_id: int):
    group = _get_or_404(group_id)
    CameraGroupRepository.delete(group_id)
    AuditRepository.record("DELETE", "camera_groups", group_id,
                           f"deleted camera group '{group['name']}'",
                           auth.current_user()["username"])
    flash(f"Camera group '{group['name']}' deleted.", "success")
    return redirect(url_for("camera_groups.index"))

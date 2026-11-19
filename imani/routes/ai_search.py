"""AI search across every saved photo and recording (Bit 13).

Two ways to search:

* **By description** — the prompt is parsed for colour words (with synonyms)
  and the rest is matched against annotations and camera metadata. The
  percentage is computed from the measured colour makeup of each frame, so it
  is reproducible and explainable.
* **By uploaded photo** — the upload's 108-bin colour histogram is compared
  with each stored frame (cosine similarity + histogram intersection).

Features are measured once when media is saved and cached in
``features_json``; older rows are measured lazily on first search.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import auth, features, media, prompt_search
from ..models import (
    AuditRepository,
    CameraRepository,
    CameraSnapshotRepository,
    CameraVideoRepository,
)

bp = Blueprint("ai_search", __name__, url_prefix="/ai-search")

UPLOAD_SUBDIR = "uploads/search"


def _library_root() -> Path:
    return Path(current_app.config["MEDIA_ROOT"]).parent


def _resolve(rel_path: str) -> Path | None:
    """Resolve a stored path, refusing anything outside the media library."""
    try:
        base = _library_root().resolve()
        target = (base / rel_path).resolve()
        if base != target and base not in target.parents:
            return None
        return target
    except Exception:
        return None


def _load_features(media_type: str, row) -> dict | None:
    """Cached features from the row, or measure the pixels and cache them."""
    raw = row["features_json"] if "features_json" in row.keys() else None
    if raw:
        try:
            parsed = json.loads(raw)
            if parsed.get("hist"):
                return parsed
        except (TypeError, ValueError):
            pass

    if media_type == "snapshot":
        target = _resolve(row["path"])
    else:
        kind = row["kind"] if "kind" in row.keys() else "video"
        if kind == "audio":
            return None
        target = _resolve(row["path"]) / "poster.jpg"
    if target is None or not target.exists():
        return None

    measured = features.extract_features(target)
    if not measured:
        return None
    payload = json.dumps(measured)
    try:
        if media_type == "snapshot":
            CameraSnapshotRepository.update(row["id"], features_json=payload)
        else:
            CameraVideoRepository.update(row["id"], features_json=payload)
    except Exception:
        pass
    return measured


def _meta_text(row) -> str:
    parts = [
        row["camera_name"] or "",
        row["source_type"] or "",
        row["description"] if "description" in row.keys() else "",
        row["path"] or "",
    ]
    return " ".join(part for part in parts if part)


def _candidate_snapshot(row) -> dict:
    return {
        "type": "snapshot",
        "id": row["id"],
        "camera_name": row["camera_name"],
        "timestamp": row["captured_at"],
        "kind": "photo",
        "description": row["description"] if "description" in row.keys() else None,
        "features": _load_features("snapshot", row),
        "thumb_url": url_for("snapshots.image", snapshot_id=row["id"]),
        "view_url": url_for("ai_search.media_detail",
                            media_type="snapshot", media_id=row["id"]),
    }


def _candidate_video(row) -> dict:
    kind = row["kind"] if "kind" in row.keys() else "manual"
    is_audio = kind == "audio"
    return {
        "type": "video",
        "id": row["id"],
        "camera_name": row["camera_name"],
        "timestamp": row["captured_at"],
        "kind": kind,
        "description": row["description"] if "description" in row.keys() else None,
        "features": _load_features("video", row),
        "thumb_url": None if is_audio else url_for("videos.poster", video_id=row["id"]),
        "view_url": url_for("videos.detail", video_id=row["id"]),
        "is_audio": is_audio,
    }


def _candidates(limit: int = 400) -> list[dict]:
    items = []
    for row in CameraSnapshotRepository.all(limit=limit):
        items.append(_candidate_snapshot(row))
    for row in CameraVideoRepository.all(limit=limit):
        items.append(_candidate_video(row))
    return items


def _swatches(features_record: dict | None) -> list[dict]:
    return (features_record or {}).get("dominant", [])[:3]


def _search_description(query: str) -> list[dict]:
    """Merge media matches and identity matches into one ranked answer."""
    results = []
    for item in _candidates():
        meta = " ".join(filter(None, [
            item["camera_name"], item["kind"], item.get("description") or "",
        ]))
        if item["features"]:
            percentage, detail = features.description_match(
                query, item["features"], meta,
            )
        else:
            percentage = round(features.keyword_score(query, meta) * 100, 1)
            detail = {"mode": "keywords", "percentage": percentage}
        if percentage <= 0:
            continue
        item = dict(item)
        item["score"] = percentage
        item["detail"] = detail
        item["swatches"] = _swatches(item["features"])
        results.append(item)

    results.extend(prompt_search.search_identities(query))
    results.sort(key=lambda row: row["score"], reverse=True)
    if results:
        results[0]["best"] = True
    return results


def _search_image(upload) -> tuple[list[dict], str | None]:
    directory = _library_root() / UPLOAD_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(
        ch for ch in (upload.filename or "upload") if ch.isalnum() or ch in "._-"
    ) or "upload"
    filename = f"search_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    target = directory / filename
    upload.save(str(target))

    query_features = features.extract_features(target)
    if not query_features:
        return [], filename

    results = []
    for item in _candidates():
        if not item["features"]:
            continue
        percentage, detail = features.image_similarity(
            query_features, item["features"],
        )
        item = dict(item)
        item["score"] = percentage
        item["detail"] = detail
        item["swatches"] = _swatches(item["features"])
        results.append(item)
    results.sort(key=lambda row: row["score"], reverse=True)
    return results, filename


@bp.route("/", methods=("GET", "POST"))
@auth.login_required
def index():
    results: list[dict] = []
    query = ""
    search_type = "description"
    uploaded = None
    searched = False

    if request.method == "POST":
        searched = True
        search_type = request.form.get("search_type", "description")
        query = (request.form.get("query") or "").strip()

        if search_type == "description" and query:
            results = _search_description(query)[:60]
            AuditRepository.record(
                "SEARCH", "media", None,
                f"AI description search: {query} ({len(results)} matches)",
                auth.current_user()["username"],
            )
        elif search_type == "image":
            upload = request.files.get("photo")
            if upload is not None and upload.filename:
                results, uploaded = _search_image(upload)
                results = results[:60]
                AuditRepository.record(
                    "SEARCH", "media", None,
                    f"AI photo search ({len(results)} matches)",
                    auth.current_user()["username"],
                )
            else:
                flash("Choose a photo to search with.", "warning")
        else:
            flash("Enter a description to search for.", "warning")

    return render_template(
        "ai_search/index.html",
        results=results,
        best_match=results[0] if results else None,
        query=query,
        search_type=search_type,
        uploaded=uploaded,
        searched=searched,
        upload_url=(
            url_for("ai_search.uploaded", filename=uploaded) if uploaded else None
        ),
    )


@bp.route("/uploads/<path:filename>")
@auth.login_required
def uploaded(filename: str):
    target = _resolve(f"{UPLOAD_SUBDIR}/{filename}")
    if target is None or not target.exists():
        abort(404)
    return send_file(str(target))


@bp.route("/media/<media_type>/<int:media_id>")
@auth.login_required
def media_detail(media_type: str, media_id: int):
    if media_type == "snapshot":
        row = CameraSnapshotRepository.get(media_id)
        if row is None:
            abort(404)
        item = _candidate_snapshot(row)
        return render_template(
            "ai_search/media_detail.html", media=row, item=item,
            media_type="snapshot",
        )
    if media_type == "video":
        row = CameraVideoRepository.get(media_id)
        if row is None:
            abort(404)
        item = _candidate_video(row)
        return render_template(
            "ai_search/media_detail.html", media=row, item=item,
            media_type="video",
        )
    abort(404)

"""Automate the Blender Baking -> Substance 3D Painter handoff."""

import hashlib
import json
import math
import os
import time
from pathlib import Path

from PySide6 import QtCore

import substance_painter.baking
import substance_painter.event
import substance_painter.export
import substance_painter.js
import substance_painter.layerstack
import substance_painter.project
import substance_painter.resource
import substance_painter.source
import substance_painter.textureset
import substance_painter.ui


REQUEST_FILENAME = ".substance_tools_request.json"
PENDING_REQUEST_FILENAME = "pending_request.json"
EXPORT_REQUEST_FILENAME = ".substance_tools_export_request.json"
EXPORT_RESULT_FILENAME = ".substance_tools_export_result.json"
CLOTH_EXPORT_PRESET_NAME = "Unreal_V2_Cloth"
CLOTH_EXPORT_CHANNELS = (
    ("SheenColor", "sRGB8"),
    ("SheenOpacity", "L8"),
    ("SheenRoughness", "L8"),
)
METADATA_CONTEXT = "SubstanceToolsBlender"
MANAGED_SOURCE_LAYER_PREFIX = "[ST Managed Source v1]"
MANAGED_SOURCE_RESOURCE_GROUP = "Substance Tools"
SOURCE_METADATA_KEYS = (
    "source_material_hashes",
    "source_normal_mesh_hashes",
    "source_normal_hashes",
    "source_normal_mesh_map_hashes",
)
_started = False
_processing = False
_active_request = None
_pending_timer = None
_pending_creation_request_id = None
_pending_creation_started_at = 0.0
_startup_resources_ready = False
_pending_replacement_blocked_reason = None
_pending_request_wait_reason = None
_project_ready_idle_scheduled = False
_project_ready_idle_generation = None
# Preserve the counter across importlib.reload().  Deferred callbacks capture a
# generation and become inert as soon as the plugin is closed or reloaded.
_plugin_generation = globals().get("_plugin_generation", 0)
_active_bake_callback = globals().get("_active_bake_callback", None)
_last_polled_pipeline_hash = None
_last_export_request_id = None
_export_processing = False
_last_busy_log_time = 0.0


def _log_file_path():
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TEMP")
        or Path.home()
    )
    return base / "SubstanceTools" / "substance_tools_timing.log"


def _normalized_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _request_marker(request):
    return request.get("request_id") or request.get("pipeline_hash")


def _is_new_create_request(request):
    return bool(
        isinstance(request, dict)
        and request.get("action") == "CREATE"
        and not request.get("spp_existed")
    )


def _same_pending_request(left, right):
    if not (_is_new_create_request(left) and _is_new_create_request(right)):
        return False
    marker = _request_marker(left)
    if not marker or marker != _request_marker(right):
        return False
    for key in ("spp", "low_fbx", "template"):
        left_value = left.get(key)
        right_value = right.get(key)
        if not left_value or not right_value:
            return False
        if _normalized_path(left_value) != _normalized_path(right_value):
            return False
    return True


def _guard_async(callback, request=None):
    """Make a Painter callback inert after close/reload or request replacement."""
    generation = _plugin_generation
    marker = _request_marker(request) if request is not None else None

    def guarded(*args, **kwargs):
        if not _started or generation != _plugin_generation:
            return None
        if request is not None:
            active = _active_request
            if (
                active is not request
                or _request_marker(active) != marker
            ):
                return None
        return callback(*args, **kwargs)

    return guarded


def _single_shot_guarded(delay_ms, callback, request=None):
    guarded = _guard_async(callback, request)
    QtCore.QTimer.singleShot(delay_ms, guarded)
    return guarded


def _execute_when_not_busy_guarded(callback, request=None):
    guarded = _guard_async(callback, request)
    substance_painter.project.execute_when_not_busy(guarded)
    return guarded


def _log(message):
    print(f"[Substance Tools] {message}")
    try:
        path = _log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{timestamp} [Substance Tools] {message}\n")
    except Exception:
        pass


def _elapsed_ms(start):
    return (time.perf_counter() - start) * 1000.0


def _request_age_ms(request):
    try:
        request_id = int(request.get("request_id", ""))
    except (TypeError, ValueError):
        return None
    return max(0.0, (time.time_ns() - request_id) / 1_000_000.0)


def _log_timing(message):
    _log(f"[timing] {message}")


def _request_candidates():
    candidates = []
    try:
        project_path = substance_painter.project.file_path()
        if project_path:
            candidates.append(Path(project_path).parent / REQUEST_FILENAME)
    except Exception:
        pass
    try:
        mesh_path = substance_painter.project.last_imported_mesh_path()
        if mesh_path:
            candidates.append(Path(mesh_path).parent / REQUEST_FILENAME)
            candidates.append(Path(mesh_path).parent.parent / "texture" / REQUEST_FILENAME)
    except Exception:
        pass
    return candidates


def _request_payload_candidates(request):
    """Return durable request copies named directly by a request payload."""
    candidates = []
    low_fbx = request.get("low_fbx")
    if low_fbx:
        candidates.append(Path(low_fbx).parent / REQUEST_FILENAME)
    texture_dir = request.get("texture_dir")
    if texture_dir:
        candidates.append(Path(texture_dir) / REQUEST_FILENAME)
    spp_path = request.get("spp")
    if spp_path:
        candidates.append(Path(spp_path).parent / REQUEST_FILENAME)
    return candidates


def _pending_request_path():
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TEMP")
        or Path.home()
    )
    return base / "SubstanceTools" / PENDING_REQUEST_FILENAME


def _load_request():
    pending_request = _load_matching_pending_request()
    if pending_request is not None:
        return pending_request
    for path in _request_candidates():
        if not path.is_file():
            continue
        try:
            request = json.loads(path.read_text(encoding="utf-8-sig"))
            if request.get("status") in {"FAILED", "SUCCESS"}:
                continue
            matched, _reason = _open_project_request_match(request)
            if not matched:
                continue
            request["_request_path"] = str(path)
            request["_loaded_perf"] = time.perf_counter()
            return request
        except (OSError, ValueError) as error:
            _log(f"Could not read {path}: {error}")
    return None


def _load_pending_request():
    path = _pending_request_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        _log(f"Could not read pending project request: {error}")
        return None


def _open_project_request_match(request):
    """Match an open Painter project to a request without trusting its name."""
    if not substance_painter.project.is_open():
        return False, "Painter has no open project"

    expected_spp = request.get("spp")
    try:
        project_path = substance_painter.project.file_path()
    except Exception:
        project_path = None
    if project_path and expected_spp and (
        _normalized_path(project_path) == _normalized_path(expected_spp)
    ):
        return True, "project path"

    # Only a newly created project can legitimately be unsaved or report its
    # template as file_path(). Existing-project actions must prove that the
    # requested .spp itself is open; matching only the imported mesh would let
    # an unrelated unsaved project overwrite that .spp on its next save.
    if not _is_new_create_request(request):
        if project_path:
            return False, f"open project path differs: {project_path}"
        return False, "existing-project request requires its requested .spp to be open"

    # Painter 12.1.3 can report template_file_path as file_path() for a newly
    # created, still-unsaved project. That path is not the project's save
    # target, so accept it only when both the exact request template and the
    # imported low mesh agree below.
    request_template = request.get("template")
    project_reports_request_template = bool(
        project_path
        and request_template
        and _normalized_path(project_path) == _normalized_path(request_template)
    )
    if project_path and not project_reports_request_template:
        return False, f"open project path differs: {project_path}"

    expected_mesh = request.get("low_fbx")
    try:
        mesh_path = substance_painter.project.last_imported_mesh_path()
    except Exception:
        mesh_path = None
    if mesh_path:
        if expected_mesh and (
            _normalized_path(mesh_path) == _normalized_path(expected_mesh)
        ):
            if project_reports_request_template:
                return True, "request template and imported mesh paths"
            return True, "imported mesh path"
        return False, f"open project mesh differs: {mesh_path}"

    return False, "Painter has not exposed the unsaved project's mesh path yet"


class _ProjectRequestMismatch(RuntimeError):
    pass


def _require_requested_project_open(request, operation):
    matched, reason = _open_project_request_match(request)
    if not matched:
        raise _ProjectRequestMismatch(
            f"Refusing to {operation}; the requested Painter project is no longer open: "
            f"{reason}"
        )


def _log_pending_request_wait(reason):
    global _pending_request_wait_reason
    if reason == _pending_request_wait_reason:
        return
    _pending_request_wait_reason = reason
    _log(f"Pending create request is waiting: {reason}")


def _load_matching_pending_request():
    """Load the CREATE ticket only after it matches the open Painter target."""
    request = _load_pending_request()
    if (
        request is None
        or request.get("status") in {"FAILED", "SUCCESS"}
        or not _is_new_create_request(request)
    ):
        return None
    matched, reason = _open_project_request_match(request)
    if not matched:
        _log_pending_request_wait(reason)
        return None

    request["_loaded_from_pending"] = True
    request["_loaded_perf"] = time.perf_counter()
    durable_request_path = None
    for path in _request_payload_candidates(request):
        if not path.is_file():
            continue
        try:
            candidate = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if _same_pending_request(candidate, request):
            durable_request_path = path
            break
    if durable_request_path is None:
        _log_pending_request_wait(
            "matching project is open, but its durable request copy is not ready"
        )
        return None
    request["_request_path"] = str(durable_request_path)
    return request


def _claim_pending_request(request):
    """Claim the exact CREATE ticket in memory; keep it durable until save."""
    global _pending_creation_request_id, _pending_creation_started_at
    global _pending_replacement_blocked_reason, _pending_request_wait_reason
    pending = _load_pending_request()
    if pending is None or not _same_pending_request(pending, request):
        return False
    matched, _reason = _open_project_request_match(pending)
    if not matched:
        return False
    _pending_creation_request_id = None
    _pending_creation_started_at = 0.0
    _pending_replacement_blocked_reason = None
    _pending_request_wait_reason = None
    request["_pending_request_claimed"] = True
    _log(f"Pending create request claimed in Painter: {request.get('spp', '')}")
    return True


def _restore_pending_without_overwrite(moved_path, pending_path):
    """Restore a moved ticket without ever replacing a newer generic ticket."""
    try:
        os.link(moved_path, pending_path)
    except FileExistsError:
        return False
    except OSError as link_error:
        if os.name != "nt":
            _log(
                "Could not restore a moved pending request without overwrite: "
                f"{link_error}; preserved it at {moved_path}"
            )
            return False
        try:
            # On Windows os.rename fails when the destination already exists,
            # providing the same no-clobber guarantee when hard links are not
            # available on the underlying filesystem.
            os.rename(moved_path, pending_path)
            return True
        except FileExistsError:
            return False
        except OSError as rename_error:
            _log(
                "Could not restore a moved pending request without overwrite: "
                f"{rename_error}; preserved it at {moved_path}"
            )
            return False

    try:
        moved_path.unlink(missing_ok=True)
    except OSError as error:
        _log(
            "Pending request was restored, but its temporary hard link could "
            f"not be removed: {error}"
        )
    return True


def _delete_claimed_pending_request(request):
    """Atomically remove only the pending CREATE ticket that just saved.

    Moving first means a concurrent Blender os.replace either wins before this
    move (and is restored after the marker mismatch) or wins after it (and stays
    at the generic pending path while this request's moved ticket is removed).
    """
    global _pending_replacement_blocked_reason, _pending_request_wait_reason
    if not request.get("_pending_request_claimed"):
        return False
    marker = _request_marker(request)
    if not marker:
        return False
    path = _pending_request_path()
    token = hashlib.sha256(str(marker).encode("utf-8")).hexdigest()[:12]
    moved_path = path.with_name(
        f".{path.name}.{token}.{os.getpid()}.{time.time_ns()}.completed"
    )
    try:
        os.replace(path, moved_path)
    except FileNotFoundError:
        return False
    except OSError as error:
        _log(f"Could not claim completed pending request for deletion: {error}")
        return False

    try:
        moved = json.loads(moved_path.read_text(encoding="utf-8-sig"))
        if not _same_pending_request(moved, request):
            if not _restore_pending_without_overwrite(moved_path, path):
                _log(
                    "A different pending request replaced the completed ticket; "
                    f"preserved the moved request at {moved_path}"
                )
            return False
        moved_path.unlink(missing_ok=True)
    except Exception as error:
        if moved_path.exists():
            _restore_pending_without_overwrite(moved_path, path)
        _log(f"Could not remove completed pending request: {error}")
        return False

    _pending_replacement_blocked_reason = None
    _pending_request_wait_reason = None
    request["_pending_request_claimed"] = False
    _log(f"Completed pending create request removed: {request.get('spp', '')}")
    return True


def _write_json(path, value):
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _normalized_resource_name(name):
    return str(name or "").lower().replace(" ", "").replace("_", "")


def _find_export_preset_url(requested_name):
    normalized_name = _normalized_resource_name(requested_name)
    preset = next(
        (
            candidate
            for candidate in substance_painter.export.list_resource_export_presets()
            if _normalized_resource_name(candidate.resource_id.name) == normalized_name
        ),
        None,
    )
    if preset is not None:
        return preset.resource_id.url()
    return None


def _export_preset_url(request):
    requested_name = request.get("preset", "Unreal_V2")
    preset_url = _find_export_preset_url(requested_name)
    if preset_url:
        return preset_url

    preset_path = Path(request.get("preset_path", ""))
    if preset_path.is_file():
        try:
            substance_painter.resource.Shelves.refresh_all()
            preset_url = _find_export_preset_url(requested_name)
            if preset_url:
                return preset_url
        except Exception as error:
            _log(f"Could not refresh Painter shelves for {requested_name}: {error}")

        try:
            resource = substance_painter.resource.import_session_resource(
                str(preset_path),
                substance_painter.resource.Usage.EXPORT,
                name=requested_name,
                group="SubstanceTools",
            )
            preset_url = resource.identifier().url()
            _log(
                f"Imported export preset {requested_name} into Painter session "
                f"from {preset_path}"
            )
            return preset_url
        except Exception as error:
            raise RuntimeError(
                f"Could not load Painter export preset {requested_name} "
                f"from {preset_path}: {error}"
            ) from error

    raise RuntimeError(
        f"Painter export preset {requested_name} was not found, and preset_path "
        f"is missing or invalid: {preset_path}"
    )


def _export_preset_candidates(request):
    inline_presets = request.get("inline_presets") or []
    inline_candidates = [
        preset
        for preset in inline_presets
        if isinstance(preset, dict) and preset.get("maps")
    ]
    candidates = []
    try:
        candidates.append(_export_preset_url(request))
    except RuntimeError:
        if not inline_candidates:
            raise
    candidates.extend(inline_candidates)
    return candidates


def _export_textures_with_preset(request, export_list, preset):
    config = {
        "exportShaderParams": False,
        "exportPath": request["texture_dir"],
        "exportList": export_list,
        "exportParameters": [{
            "parameters": {
                "fileFormat": "png",
                "bitDepth": "8",
                "dithering": False,
                "paddingAlgorithm": "infinite",
            }
        }],
    }
    if isinstance(preset, dict):
        preset_name = str(preset.get("name") or request.get("preset") or "SubstanceToolsExport")
        config["defaultExportPreset"] = preset_name
        config["exportPresets"] = [preset]
    else:
        config["defaultExportPreset"] = preset
    return substance_painter.export.export_project_textures(config)


def _is_cloth_export_request(request):
    if str(request.get("preset") or "") == CLOTH_EXPORT_PRESET_NAME:
        return True
    for preset in request.get("inline_presets") or []:
        if isinstance(preset, dict) and str(preset.get("name") or "") == CLOTH_EXPORT_PRESET_NAME:
            return True
    return False


def _enum_member(enum_type, name):
    value = getattr(enum_type, name, None)
    if value is None:
        members = getattr(enum_type, "__members__", {})
        value = members.get(name)
    if value is None:
        raise RuntimeError(f"Painter enum member is missing: {enum_type}.{name}")
    return value


def _stack_root_path(texture_set, stack):
    stack_name = stack.name()
    return f"{texture_set.name}/{stack_name}" if stack_name else str(texture_set.name)


def _ensure_cloth_export_channels(request):
    if not _is_cloth_export_request(request):
        return None

    audit = {
        "preset": CLOTH_EXPORT_PRESET_NAME,
        "required": [name for name, _format_name in CLOTH_EXPORT_CHANNELS],
        "added": [],
        "already_enabled": [],
    }
    errors = []

    for texture_set in substance_painter.textureset.all_texture_sets():
        for stack in texture_set.all_stacks():
            root_path = _stack_root_path(texture_set, stack)
            for channel_name, format_name in CLOTH_EXPORT_CHANNELS:
                try:
                    channel_type = _enum_member(
                        substance_painter.textureset.ChannelType,
                        channel_name,
                    )
                    if stack.has_channel(channel_type):
                        audit["already_enabled"].append({
                            "rootPath": root_path,
                            "channel": channel_name,
                        })
                        continue
                    channel_format = _enum_member(
                        substance_painter.textureset.ChannelFormat,
                        format_name,
                    )
                    stack.add_channel(channel_type, channel_format)
                    audit["added"].append({
                        "rootPath": root_path,
                        "channel": channel_name,
                        "format": format_name,
                    })
                except Exception as error:
                    errors.append(f"{root_path}:{channel_name}: {error}")

    if errors:
        audit["errors"] = errors
        raise RuntimeError(
            "Could not enable required Painter cloth channels: "
            + "; ".join(errors)
        )

    if audit["added"]:
        substance_painter.project.save()
        _log(
            "Enabled Painter cloth channel(s): "
            + ", ".join(
                f"{item['rootPath']}:{item['channel']}"
                for item in audit["added"]
            )
        )
    else:
        _log("Painter cloth channels already enabled")
    return audit


def _strip_texture_set_prefixes():
    texture_sets = substance_painter.textureset.all_texture_sets()
    current_names = {str(texture_set.name) for texture_set in texture_sets}
    renamed = []
    for texture_set in texture_sets:
        current_name = str(texture_set.name)
        if not current_name.startswith("M_"):
            continue
        target_name = current_name[2:]
        if target_name in current_names:
            raise RuntimeError(
                f"Cannot rename Texture Set '{current_name}' to '{target_name}': "
                "target name already exists"
            )
        texture_set.name = target_name
        current_names.remove(current_name)
        current_names.add(target_name)
        renamed.append((current_name, target_name))
    if renamed:
        _log(
            "Renamed Painter Texture Set(s): "
            + ", ".join(f"{old} -> {new}" for old, new in renamed)
        )
    return renamed


def _normalize_texture_set_names():
    """Drop the M_ prefix from Texture Sets, logging (not raising) on failure.

    The strict pre-bake path calls _strip_texture_set_prefixes() directly so a
    naming failure aborts instead of baking the wrong target. This forgiving
    wrapper remains useful for idempotent save/reload/export paths.
    """
    try:
        _strip_texture_set_prefixes()
    except Exception as error:
        _log(f"Could not normalize Painter Texture Set names: {error}")


def _matching_request_paths(request):
    marker = request.get("request_id") or request.get("pipeline_hash")
    paths = []
    if request.get("_request_path"):
        paths.append(Path(request["_request_path"]))
    paths.extend(_request_payload_candidates(request))
    paths.extend(_request_candidates())
    result = []
    seen = set()
    for path in paths:
        normalized = _normalized_path(path)
        if normalized in seen or not path.is_file():
            continue
        seen.add(normalized)
        try:
            candidate = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        candidate_marker = candidate.get("request_id") or candidate.get("pipeline_hash")
        if candidate_marker == marker:
            result.append(path)
    return result


def _request_result_payload(request):
    saved = dict(request)
    for key in (
        "_request_path",
        "_loaded_perf",
        "_accepted_perf",
        "_reload_started_perf",
        "_bake_started_perf",
        "_needs_bake",
        "_low_reloaded",
        "_save_retry_count",
        "_loaded_from_pending",
        "_pending_request_claimed",
    ):
        saved.pop(key, None)
    return saved


def _mark_request_failed(request, message):
    try:
        request_paths = _matching_request_paths(request)
        if not request_paths:
            return
        saved = _request_result_payload(request)
        saved["status"] = "FAILED"
        saved["failure"] = message
        for request_path in request_paths:
            _write_json(request_path, saved)
    except Exception as error:
        _log(f"Could not mark request failed: {error}")


def _mark_request_success(request):
    try:
        request_paths = _matching_request_paths(request)
        if not request_paths:
            return False
        saved = _request_result_payload(request)
        saved["status"] = "SUCCESS"
        saved.pop("failure", None)
        for request_path in request_paths:
            _write_json(request_path, saved)
        return True
    except Exception as error:
        _log(f"Could not mark request successful: {error}")
        return False


def _request_matches_saved_metadata(metadata, request):
    for key in (
        "pipeline_hash",
        "low_hash",
        "high_hash",
        "settings_hash",
        "base_color_hashes",
        "alpha_color_hashes",
        "back_normal_hashes",
    ):
        if _metadata_value(metadata, key) != _request_value(request, key):
            return False
    for key in SOURCE_METADATA_KEYS:
        if (
            key in request
            and _metadata_value(metadata, key) != _request_value(request, key)
        ):
            return False
    return True


def _process_export_request():
    global _last_export_request_id, _export_processing
    if _export_processing or not substance_painter.project.is_open():
        return
    project_path = substance_painter.project.file_path()
    if not project_path:
        return
    request_path = Path(project_path).parent / EXPORT_REQUEST_FILENAME
    if not request_path.is_file():
        return
    claimed_request_path = request_path.with_name(
        f"{request_path.name}.{os.getpid()}.processing"
    )
    try:
        os.replace(request_path, claimed_request_path)
    except OSError:
        return
    try:
        request = json.loads(claimed_request_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        _log(f"Could not read Painter export request: {error}")
        claimed_request_path.unlink(missing_ok=True)
        return
    request_id = request.get("request_id")
    if not request_id or request_id == _last_export_request_id:
        claimed_request_path.unlink(missing_ok=True)
        return
    if _normalized_path(project_path) != _normalized_path(request.get("spp", "")):
        claimed_request_path.unlink(missing_ok=True)
        return
    if substance_painter.project.is_busy():
        os.replace(claimed_request_path, request_path)
        return

    _last_export_request_id = request_id
    _export_processing = True
    result_path = Path(request["texture_dir"]) / EXPORT_RESULT_FILENAME
    try:
        _normalize_texture_set_names()
        source_state_receipt = (
            _audit_expected_source_state(request["expected_source_state"])
            if request.get("expected_source_state")
            else None
        )
        channel_audit = _ensure_cloth_export_channels(request)
        export_list = []
        for texture_set in substance_painter.textureset.all_texture_sets():
            for stack in texture_set.all_stacks():
                stack_name = stack.name()
                root_path = (
                    f"{texture_set.name}/{stack_name}"
                    if stack_name
                    else texture_set.name
                )
                export_list.append({"rootPath": root_path})
        errors = []
        result = None
        for preset in _export_preset_candidates(request):
            try:
                result = _export_textures_with_preset(request, export_list, preset)
            except Exception as error:
                errors.append(str(error))
                continue
            status_name = getattr(result.status, "name", str(result.status))
            if status_name.lower() == "success":
                break
            errors.append(result.message)
        if result is None:
            raise RuntimeError("; ".join(errors) or "No Painter export preset candidate worked")
        status_name = getattr(result.status, "name", str(result.status))
        success = status_name.lower() == "success"
        _write_json(result_path, {
            "request_id": request_id,
            "status": "SUCCESS" if success else "ERROR",
            "message": result.message,
            "textures": {
                "/".join(key): value
                for key, value in result.textures.items()
            },
            "channel_audit": channel_audit,
            "source_state_receipt": source_state_receipt,
        })
        _log(
            f"Unreal_V2 texture export {'completed' if success else 'failed'}"
        )
    except Exception as error:
        _write_json(result_path, {
            "request_id": request_id,
            "status": "ERROR",
            "message": str(error),
        })
        _log(f"Could not export textures with Unreal_V2: {error}")
    finally:
        _export_processing = False
        claimed_request_path.unlink(missing_ok=True)


def _all_shelves_idle():
    """Return True once Painter's resource services finished startup crawling."""
    try:
        shelves = substance_painter.resource.Shelves.all()
        return bool(shelves) and not any(shelf.is_crawling() for shelf in shelves)
    except Exception:
        return False


def _pending_target_is_open(request):
    """Check whether Painter already has the project represented by request open."""
    matched, _reason = _open_project_request_match(request)
    return matched


def _mark_startup_resources_ready(_event=None):
    """Unlock pending project creation after Painter's shelves finish crawling."""
    global _startup_resources_ready
    if not _started:
        return
    if not _all_shelves_idle():
        return
    if not _startup_resources_ready:
        _log("Painter startup resources are ready")
    _startup_resources_ready = True
    _single_shot_guarded(0, _create_pending_project)


def _create_pending_project():
    global _pending_creation_request_id, _pending_creation_started_at
    global _pending_replacement_blocked_reason
    if not _started:
        return
    request = _load_pending_request()
    if (
        request is None
        or request.get("status") in {"FAILED", "SUCCESS"}
        or not _is_new_create_request(request)
    ):
        return
    if not _startup_resources_ready:
        return

    if substance_painter.project.is_open():
        if _pending_target_is_open(request):
            if substance_painter.project.is_in_edition_state():
                _pending_creation_request_id = None
                _pending_creation_started_at = 0.0
                _pending_replacement_blocked_reason = None
                _log_pending_request_wait(
                    f"project reached edition state; awaiting request claim: {request['spp']}"
                )
            return

        # Painter can restore its previous session after a no-argument launch.
        # Replace that restored project only when it has no unsaved changes.
        try:
            if (
                not substance_painter.project.is_in_edition_state()
                or substance_painter.project.is_busy()
            ):
                return
            project_path = substance_painter.project.file_path() or "<unsaved project>"
            if substance_painter.project.needs_saving():
                reason = f"restored project has unsaved changes: {project_path}"
                if reason != _pending_replacement_blocked_reason:
                    _log(
                        "Pending Blender project is waiting because the restored "
                        f"Painter project has unsaved changes: {project_path}"
                    )
                    _pending_replacement_blocked_reason = reason
                return
            substance_painter.project.close()
            _pending_replacement_blocked_reason = None
            _log(f"Closed restored Painter project before Blender handoff: {project_path}")
        except Exception as error:
            reason = str(error)
            if reason != _pending_replacement_blocked_reason:
                _log(f"Could not replace restored Painter project yet: {error}")
                _pending_replacement_blocked_reason = reason
        return

    template_path = Path(request.get("template", ""))
    if not template_path.is_file():
        _log(f"Unreal Engine template was not found: {template_path}")
        return
    request_id = request.get("request_id") or request.get("pipeline_hash")
    if (
        request_id == _pending_creation_request_id
        and time.perf_counter() - _pending_creation_started_at < 30.0
    ):
        return
    try:
        settings = substance_painter.project.Settings(
            default_save_path=request["spp"],
            export_path=request["texture_dir"],
            default_texture_resolution=int(request["settings"]["resolution"]),
            auto_unwrap_settings=substance_painter.project.AutoUnwrapSettings(
                recompute_seams=False,
                recompute_uv_islands=False,
                recompute_packing=False,
            ),
        )
        _pending_creation_request_id = request_id
        _pending_creation_started_at = time.perf_counter()
        substance_painter.project.create(
            request["low_fbx"],
            template_file_path=str(template_path),
            settings=settings,
        )
        # project.create() returns before Texture Sets exist. The pending ticket
        # therefore remains authoritative until _on_project_ready accepts it;
        # names are normalized immediately before the first bake.
        _log(f"Project creation submitted with Painter's Unreal Engine template: {request['spp']}")
    except Exception as error:
        _pending_creation_request_id = None
        _pending_creation_started_at = 0.0
        _log(f"Could not create project from Unreal Engine template: {error}")


def _poll_requests():
    if not _started:
        return
    _create_pending_project()
    _process_export_request()
    if (
        substance_painter.project.is_open()
        and substance_painter.project.is_in_edition_state()
    ):
        request = _load_request()
        if request is None:
            return
        request_marker = request.get("request_id") or request.get("pipeline_hash")
        if request_marker == _last_polled_pipeline_hash:
            return
        _on_project_ready()


def _find_property(properties, *needles):
    normalized_needles = tuple(needle.lower().replace(" ", "") for needle in needles)
    for prop in properties.values():
        haystack = f"{prop.short_name()} {prop.label()}".lower().replace(" ", "")
        if any(needle in haystack for needle in normalized_needles):
            return prop
    return None


def _find_property_containing_all(properties, *needles):
    normalized_needles = tuple(needle.lower().replace(" ", "") for needle in needles)
    for prop in properties.values():
        haystack = f"{prop.short_name()} {prop.label()}".lower().replace(" ", "")
        if all(needle in haystack for needle in normalized_needles):
            return prop
    return None


def _enum_value_containing(prop, *needles):
    if prop is None:
        return None
    normalized_needles = tuple(needle.lower().replace(" ", "") for needle in needles)
    for label, value in prop.enum_values().items():
        normalized_label = label.lower().replace(" ", "")
        if all(needle in normalized_label for needle in normalized_needles):
            return value
    return None


def _antialiasing_property(properties):
    return (
        _find_property_containing_all(properties, "anti", "alias")
        or _find_property(properties, "antialias", "supersampling", "subsampling")
    )


def _antialiasing_value(prop, requested):
    requested = str(requested or "NONE").upper()
    if prop is None:
        return None
    if requested == "NONE":
        for needles in (
            ("none",),
            ("no", "anti"),
            ("no", "sub"),
            ("disabled",),
            ("off",),
            ("1",),
        ):
            value = _enum_value_containing(prop, *needles)
            if value is not None:
                return value
        return None
    samples = requested[1:] if requested.startswith("X") else requested
    return _enum_value_containing(prop, samples)


def _enum_label_for_value(prop, value):
    if prop is None:
        return None
    for label, candidate in prop.enum_values().items():
        if candidate == value:
            return str(label)
    return None


def _mesh_map_usages(names):
    usages = []
    entries = substance_painter.textureset.MeshMapUsage.__entries
    for name in names:
        entry = entries.get(name)
        if entry:
            usages.append(entry[0])
    return usages


def _assign_back_normal_mesh_map(texture_set, normal_plan):
    normal_usage = substance_painter.textureset.MeshMapUsage.Normal
    texture_set_name = _texture_set_name(texture_set)
    if normal_plan.get("_managed_source_normal"):
        basis = str(normal_plan.get("basis", "LOW_TANGENT")).upper()
        convention = str(
            normal_plan.get("normal_convention", "DIRECTX")
        ).upper()
        if basis not in {"LOW_TANGENT", "LOW-TANGENT", "LOWTANGENT"}:
            _log(
                f"{texture_set_name}: source Normal basis '{basis}' is not "
                "LOW_TANGENT; Normal baker stays enabled"
            )
            return False
        if convention not in {"DIRECTX", "DIRECT_X", "DX", "-Y"}:
            _log(
                f"{texture_set_name}: source Normal convention '{convention}' "
                "is not DirectX/-Y for the Unreal Painter project; "
                "Normal baker stays enabled"
            )
            return False
    source_name = str(normal_plan.get("source_texture_set", ""))
    source_path = str(normal_plan.get("source_normal_texture", "") or _source_path(normal_plan))
    if normal_plan.get("_managed_source_normal") and not (
        source_path and Path(source_path).is_file()
    ):
        _log(
            f"{texture_set_name}: managed source Normal must name an existing file"
        )
        return False
    resource_id = None
    resource_name = None
    try:
        current_resource_id = texture_set.get_mesh_map_resource(normal_usage)
    except Exception:
        current_resource_id = None

    if source_name:
        try:
            source_set = substance_painter.textureset.TextureSet.from_name(source_name)
            resource_id = source_set.get_mesh_map_resource(normal_usage)
        except Exception as error:
            _log(
                f"{texture_set_name}: could not read Normal mesh map "
                f"from {source_name}: {error}"
            )

    if resource_id is None and source_path and Path(source_path).is_file():
        try:
            resource_token = "".join(
                char if char.isalnum() else "_"
                for char in _canonical_painter_set_name(texture_set_name)
            ).strip("_")
            resource_name = (
                f"ST_{resource_token}_SourceNormal_{_file_sha256(source_path)[:12]}"
                if normal_plan.get("_managed_source_normal")
                else f"{texture_set_name}_SourceNormalMeshMap"
            )
            if (
                normal_plan.get("_managed_source_normal")
                and getattr(current_resource_id, "name", None) == resource_name
            ):
                resource_id = current_resource_id
            else:
                imported = substance_painter.resource.import_project_resource(
                    source_path,
                    substance_painter.resource.Usage.TEXTURE,
                    name=resource_name,
                    group=MANAGED_SOURCE_RESOURCE_GROUP,
                )
                resource_id = imported.identifier()
        except Exception as error:
            _log(f"{texture_set_name}: could not import Normal mesh map: {error}")

    if resource_id is None:
        _log(
            f"{texture_set_name}: source Normal mesh map was not found; "
            "Normal baker stays enabled"
        )
        return False

    try:
        if current_resource_id != resource_id:
            texture_set.set_mesh_map_resource(normal_usage, resource_id)
        if normal_plan.get("_managed_source_normal"):
            assigned_resource = texture_set.get_mesh_map_resource(normal_usage)
            source_sha256 = _file_sha256(source_path)
            resource_identity = _resource_identity_text(assigned_resource)
            if resource_name not in resource_identity:
                raise RuntimeError(
                    "assigned Normal resource identity does not match its source digest"
                )
            normal_plan["_assignment_receipt"] = {
                "source_sha256": source_sha256,
                "resource_name": resource_name,
                "resource_identity": resource_identity,
            }
            _log(
                f"{texture_set_name}: using {source_name or source_path} as the "
                "low-basis Normal mesh map; its Normal baker is disabled"
            )
        else:
            _log(
                f"{texture_set_name}: using {source_name or source_path} as "
                "Normal mesh map; its Normal baker is disabled"
            )
        return True
    except Exception as error:
        _log(f"{texture_set_name}: could not assign Normal mesh map: {error}")
        return False


def _metadata_value(metadata, key):
    value = metadata.get(key)
    return "" if value is None else value


def _request_value(request, key):
    value = request.get(key, "")
    return "" if value is None else value


def _texture_set_name(texture_set):
    for attribute in ("name", "display_name"):
        value = getattr(texture_set, attribute, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if value:
            return str(value)
    return str(texture_set)


def _normalized_texture_set_name(name):
    value = str(name)
    if value.startswith("M_"):
        value = value[2:]
    return "".join(char for char in value.lower() if char.isalnum())


def _texture_set_matches(texture_set_name, names):
    normalized = _normalized_texture_set_name(texture_set_name)
    return any(
        normalized == _normalized_texture_set_name(name)
        for name in names
    )


_SOURCE_ROLE_ALIASES = {
    "basecolor": "BaseColor",
    "basecolour": "BaseColor",
    "color": "BaseColor",
    "colour": "BaseColor",
    "albedo": "BaseColor",
    "diffuse": "BaseColor",
    "extrar": "ExtraR",
    "ambientocclusiontransport": "ExtraR",
    "ao": "AO",
    "ambientocclusion": "AO",
    "roughness": "Roughness",
    "metallic": "Metallic",
    "metalness": "Metallic",
    "extra": "Extra",
}


def _canonical_source_role(value):
    """Return the request-contract role while keeping raw Extra distinguishable."""
    token = "".join(char for char in str(value or "").lower() if char.isalnum())
    for suffix in ("texturepath", "imagepath", "filepath", "path", "map"):
        if token.endswith(suffix):
            token = token[:-len(suffix)]
            break
    return _SOURCE_ROLE_ALIASES.get(token)


def _source_path(value):
    if isinstance(value, (str, os.PathLike)):
        return str(value)
    if not isinstance(value, dict):
        return ""
    for key in (
        "path",
        "file",
        "image",
        "texture",
        "source_path",
        "source_texture",
        "source_normal_texture",
        "normal",
        "Normal",
        "normal_map",
    ):
        candidate = value.get(key)
        if isinstance(candidate, (str, os.PathLike)) and str(candidate):
            return str(candidate)
    return ""


def _store_source_material_entry(result, texture_set, role, value):
    canonical_role = _canonical_source_role(role)
    image_path = _source_path(value)
    texture_set = str(texture_set or "")
    if not texture_set or not canonical_role or not image_path:
        return
    result.setdefault(texture_set, {})[canonical_role] = image_path


def _normalize_source_material_maps(value):
    """Normalize supported JSON shapes to texture-set -> role -> image path.

    The Blender side historically emitted role-specific dictionaries while the
    Meshy pipeline emits one dictionary per Texture Set. Accept both shapes so
    old requests stay valid during the contract migration.
    """
    result = {}
    if not value:
        return result
    if isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict):
                continue
            texture_set = entry.get("texture_set") or entry.get("textureSet") or entry.get("name")
            role = entry.get("role") or entry.get("channel")
            if role:
                _store_source_material_entry(result, texture_set, role, entry)
                continue
            for key, candidate in entry.items():
                if key in {"texture_set", "textureSet", "name", "hash", "sha256"}:
                    continue
                _store_source_material_entry(result, texture_set, key, candidate)
        return result
    if not isinstance(value, dict):
        return result
    wrapped = value.get("texture_sets") or value.get("textureSets")
    if isinstance(wrapped, (dict, list)):
        return _normalize_source_material_maps(wrapped)
    for outer_key, outer_value in value.items():
        outer_role = _canonical_source_role(outer_key)
        if outer_role:
            if isinstance(outer_value, dict) and (
                outer_value.get("texture_set") or outer_value.get("textureSet")
            ):
                texture_set = outer_value.get("texture_set") or outer_value.get("textureSet")
                _store_source_material_entry(result, texture_set, outer_role, outer_value)
            elif isinstance(outer_value, dict):
                for texture_set, candidate in outer_value.items():
                    _store_source_material_entry(result, texture_set, outer_role, candidate)
            continue
        texture_set = outer_key
        if isinstance(outer_value, list):
            for entry in outer_value:
                if not isinstance(entry, dict):
                    continue
                role = entry.get("role") or entry.get("channel")
                if role:
                    _store_source_material_entry(result, texture_set, role, entry)
            continue
        if not isinstance(outer_value, dict):
            continue
        explicit_role = outer_value.get("role") or outer_value.get("channel")
        if explicit_role:
            _store_source_material_entry(result, texture_set, explicit_role, outer_value)
            continue
        for role, candidate in outer_value.items():
            _store_source_material_entry(result, texture_set, role, candidate)
    return result


def _normal_plan(value):
    if isinstance(value, (str, os.PathLike)):
        return {"source_normal_texture": str(value)}
    if not isinstance(value, dict):
        return {}
    plan = dict(value)
    image_path = _source_path(value)
    if image_path:
        plan["source_normal_texture"] = image_path
    return plan if plan.get("source_normal_texture") or plan.get("source_texture_set") else {}


def _normalize_source_normal_mesh_maps(value):
    """Normalize source Normal requests to texture-set -> assignment plan."""
    result = {}
    if not value:
        return result
    if isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict):
                continue
            texture_set = entry.get("texture_set") or entry.get("textureSet") or entry.get("name")
            plan = _normal_plan(entry)
            if texture_set and plan:
                result[str(texture_set)] = plan
        return result
    if not isinstance(value, dict):
        return result
    wrapped = value.get("texture_sets") or value.get("textureSets")
    if isinstance(wrapped, (dict, list)):
        return _normalize_source_normal_mesh_maps(wrapped)
    normal_first = next(
        (
            candidate
            for key, candidate in value.items()
            if _canonical_source_role(key) is None
            and "".join(char for char in str(key).lower() if char.isalnum())
            in {"normal", "normalmeshmap", "normalmap"}
        ),
        None,
    )
    if isinstance(normal_first, dict):
        return _normalize_source_normal_mesh_maps(normal_first)
    for texture_set, candidate in value.items():
        plan = _normal_plan(candidate)
        if plan:
            result[str(texture_set)] = plan
    return result


def _entry_for_texture_set(entries, texture_set_name):
    if not isinstance(entries, dict):
        return None
    return next(
        (
            entry
            for name, entry in entries.items()
            if _texture_set_matches(texture_set_name, [name])
        ),
        None,
    )


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_plan_digest(plan):
    digest = hashlib.sha256()
    for role, image_path in sorted(plan.items()):
        digest.update(role.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_sha256(image_path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _configure_baking(request):
    configure_started = time.perf_counter()
    settings = request["settings"]
    resolution = int(settings.get("resolution", 2048))
    resolution_started = time.perf_counter()
    texture_sets = substance_painter.textureset.all_texture_sets()
    substance_painter.textureset.set_resolutions(
        texture_sets,
        substance_painter.textureset.Resolution(resolution, resolution),
    )
    _log_timing(
        f"texture set listing/resolution took {_elapsed_ms(resolution_started):.1f} ms "
        f"for {len(texture_sets)} Texture Set(s)"
    )
    unlink_started = time.perf_counter()
    try:
        substance_painter.baking.unlink_all_common_parameters()
    except Exception as error:
        _log(f"Could not unlink common baking parameters: {error}")
    finally:
        _log_timing(f"unlink common baking parameters took {_elapsed_ms(unlink_started):.1f} ms")

    enabled_maps = _mesh_map_usages(settings.get("mesh_maps", []))
    low_as_high_texture_sets = [str(name) for name in settings.get("low_as_high_texture_sets", [])]
    back_normal_mesh_maps = settings.get("back_normal_mesh_maps", {})
    source_normal_mesh_maps = _normalize_source_normal_mesh_maps(
        request.get("source_normal_mesh_maps")
        or settings.get("source_normal_mesh_maps")
    )
    for source_normal_plan in source_normal_mesh_maps.values():
        source_normal_plan["_managed_source_normal"] = True
    rebake_texture_sets = [str(name) for name in request.get("rebake_texture_sets", [])]
    high_entry_by_texture_set = {
        str(entry.get("texture_set", "")): entry
        for entry in request.get("high_entries", [])
        if entry.get("texture_set")
    }
    high_path = request.get("high_fbx", "")
    assigned_source_normals = []
    managed_source_normal_assignments = {}
    omitted_normal_bakers = []
    strict_settings = bool(request.get("strict_bake_settings"))
    requested_settings = {
        "antialiasing": settings.get("antialiasing", "NONE"),
        "match": settings.get("match"),
        "id_source": settings.get("id_source", "FACE_SETS"),
    }
    strict_expected = {
        "antialiasing": "X2",
        "match": "BY_MESH_NAME",
        "id_source": "MATERIAL_COLOR",
    }
    if strict_settings and requested_settings != strict_expected:
        raise RuntimeError(
            "Strict Meshy bake settings are not 2x/By Mesh Name/Material Color: "
            f"{requested_settings}"
        )
    settings_receipt = {
        "contract": "meshy-bake-settings-v1" if strict_settings else "legacy",
        "strict": strict_settings,
        "requested": requested_settings,
        "texture_sets": {},
    }

    for texture_set in texture_sets:
        texture_set_started = time.perf_counter()
        texture_set_name = _texture_set_name(texture_set)
        should_bake = (
            not rebake_texture_sets
            or _texture_set_matches(texture_set_name, rebake_texture_sets)
        )
        use_low_as_high = _texture_set_matches(texture_set_name, low_as_high_texture_sets)
        params = substance_painter.baking.BakingParameters.from_texture_set(texture_set)
        params.set_textureset_enabled(should_bake)
        normal_plan = (
            _entry_for_texture_set(source_normal_mesh_maps, texture_set_name)
            or _entry_for_texture_set(back_normal_mesh_maps, texture_set_name)
        )
        normal_assigned = bool(
            normal_plan and _assign_back_normal_mesh_map(texture_set, normal_plan)
        )
        if normal_assigned:
            if normal_plan.get("_managed_source_normal"):
                assigned_source_normals.append(texture_set_name)
                managed_source_normal_assignments[texture_set_name] = dict(
                    normal_plan.get("_assignment_receipt") or {}
                )
        if not should_bake:
            params.set_enabled_bakers([])
            try:
                params.set_enabled_uv_tiles([])
            except Exception as error:
                _log(f"Texture Set '{texture_set_name}': could not disable UV tiles: {error}")
            _log(
                f"Texture Set '{texture_set_name}': skip, "
                f"{'Low Poly as High' if use_low_as_high else 'High-to-Low'}"
            )
            _log_timing(
                f"configured Texture Set '{texture_set_name}' in "
                f"{_elapsed_ms(texture_set_started):.1f} ms"
            )
        high_entry = next(
            (
                entry for name, entry in high_entry_by_texture_set.items()
                if _texture_set_matches(texture_set_name, [name])
            ),
            None,
        )
        texture_high_paths = []
        if high_entry:
            texture_high_paths = [
                str(path)
                for path in high_entry.get("fbxs", [])
                if path
            ]
            if not texture_high_paths and high_entry.get("fbx"):
                texture_high_paths = [str(high_entry.get("fbx"))]
        elif high_path:
            texture_high_paths = [str(high_path)]
        texture_high_urls = [
            QtCore.QUrl.fromLocalFile(str(Path(path).resolve())).toString()
            for path in texture_high_paths
        ]
        use_low_as_high = use_low_as_high or not bool(texture_high_urls)
        texture_set_enabled_maps = list(enabled_maps)
        if normal_assigned:
            normal_usage = substance_painter.textureset.MeshMapUsage.Normal
            texture_set_enabled_maps = [
                usage for usage in texture_set_enabled_maps
                if usage != normal_usage
            ]
            omitted_normal_bakers.append(texture_set_name)
        params.set_enabled_bakers(texture_set_enabled_maps if should_bake else [])
        common = params.common()
        changes = {}

        output_size = common.get("OutputSize")
        if output_size is not None:
            exponent = int(math.log2(resolution))
            changes[output_size] = (exponent, exponent)
        low_as_high = (
            common.get("LowAsHigh")
            or _find_property_containing_all(common, "low", "high")
            or _find_property_containing_all(common, "use", "low", "poly", "high")
        )
        high_mesh = common.get("HipolyMesh") or _find_property_containing_all(
            common,
            "high",
            "mesh",
        )
        if low_as_high is not None:
            changes[low_as_high] = use_low_as_high
        if high_mesh is not None and not use_low_as_high:
            changes[high_mesh] = "|".join(texture_high_urls)

        cage = _find_property(common, "cagemode")
        automatic_cage = _enum_value_containing(cage, "automatic")
        if automatic_cage is not None:
            changes[cage] = automatic_cage

        match = _find_property(common, "match")
        match_value = None
        if match is not None:
            if settings.get("match") == "BY_MESH_NAME":
                match_value = _enum_value_containing(match, "mesh", "name")
            else:
                match_value = _enum_value_containing(match, "always")
            if match_value is not None:
                changes[match] = match_value

        antialiasing = _antialiasing_property(common)
        antialiasing_value = _antialiasing_value(
            antialiasing,
            settings.get("antialiasing", "NONE"),
        )
        if antialiasing_value is not None:
            changes[antialiasing] = antialiasing_value
        elif settings.get("antialiasing", "NONE") != "NONE":
            _log(
                f"Texture Set '{texture_set_name}': could not find "
                f"antialiasing value {settings.get('antialiasing')}"
            )

        id_params = params.baker(substance_painter.textureset.MeshMapUsage.ID)
        id_source_property = _find_property(
            id_params, "colorsource", "idsource", "sourcecolor"
        )
        requested_id_source = settings.get("id_source", "FACE_SETS")
        if requested_id_source in {"FACE_SETS", "VERTEX_COLOR"}:
            id_source_value = _enum_value_containing(
                id_source_property, "vertex", "color"
            )
        else:
            id_source_value = _enum_value_containing(
                id_source_property, "material", "color"
            )
        if id_source_value is not None:
            changes[id_source_property] = id_source_value

        unresolved = []
        if output_size is None:
            unresolved.append(f"resolution={resolution}")
        if match is None or match_value is None:
            unresolved.append("match=By Mesh Name")
        if antialiasing is None or antialiasing_value is None:
            unresolved.append("antialiasing=2x")
        if id_source_property is None or id_source_value is None:
            unresolved.append("id_source=Material Color")
        if strict_settings and unresolved:
            raise RuntimeError(
                f"Texture Set '{texture_set_name}' cannot resolve strict bake settings: "
                + ", ".join(unresolved)
            )

        substance_painter.baking.BakingParameters.set(changes)
        settings_receipt["texture_sets"][texture_set_name] = {
            "configured": True,
            "set_call_succeeded": True,
            "match": requested_settings["match"],
            "antialiasing": requested_settings["antialiasing"],
            "id_source": requested_settings["id_source"],
            "resolution": resolution,
            "observed_labels": {
                "match": _enum_label_for_value(match, match_value),
                "antialiasing": _enum_label_for_value(
                    antialiasing,
                    antialiasing_value,
                ),
                "id_source": _enum_label_for_value(
                    id_source_property,
                    id_source_value,
                ),
            },
        }
        _log(
            f"Texture Set '{texture_set_name}': "
            f"{'REBAKE' if should_bake else 'skip'}, "
            f"{'Low Poly as High' if use_low_as_high else 'High-to-Low'}"
        )
        _log_timing(
            f"configured Texture Set '{texture_set_name}' in "
            f"{_elapsed_ms(texture_set_started):.1f} ms"
        )

    _log(
        f"Configured {len(texture_sets)} Texture Set(s), "
        f"{resolution}px, antialiasing={settings.get('antialiasing', 'NONE')}, "
        f"match={settings.get('match')}"
    )
    request["source_normal_mesh_map_result"] = {
        "assigned_texture_sets": assigned_source_normals,
        "assigned_count": len(assigned_source_normals),
        "assignments": managed_source_normal_assignments,
        "normal_baker_omitted_texture_sets": omitted_normal_bakers,
        "normal_baker_omitted_count": len(omitted_normal_bakers),
    }
    settings_receipt["configured_texture_set_count"] = sum(
        1
        for value in settings_receipt["texture_sets"].values()
        if value.get("configured")
    )
    settings_receipt["exact"] = (
        not strict_settings
        or settings_receipt["configured_texture_set_count"] > 0
        and all(
            value.get("configured") and value.get("set_call_succeeded")
            for value in settings_receipt["texture_sets"].values()
        )
    )
    if strict_settings and not settings_receipt["exact"]:
        raise RuntimeError('Strict Meshy bake settings were not applied to every Texture Set')
    request["bake_settings_result"] = settings_receipt
    _log_timing(f"configure baking total {_elapsed_ms(configure_started):.1f} ms")


def _apply_base_color_layers(request):
    base_color_maps = request.get("base_color_maps", {})
    if not base_color_maps:
        return

    applied = 0
    for texture_set in substance_painter.textureset.all_texture_sets():
        texture_set_name = texture_set.name
        image_path = base_color_maps.get(texture_set_name)
        if not image_path or not Path(image_path).is_file():
            continue
        imported = substance_painter.resource.import_project_resource(
            image_path,
            substance_painter.resource.Usage.TEXTURE,
            name=f"{texture_set_name}_BlenderBaseColor",
            group="Substance Tools",
        )
        resource_id = imported.identifier()
        for stack in texture_set.all_stacks():
            root_nodes = substance_painter.layerstack.get_root_layer_nodes(stack)
            layer_name = "Blender High Base Color"
            fill_layer = next(
                (
                    node
                    for node in root_nodes
                    if isinstance(node, substance_painter.layerstack.FillLayerNode)
                    and node.get_name() == layer_name
                ),
                None,
            )
            if fill_layer is None:
                position = (
                    substance_painter.layerstack.InsertPosition.below_node(root_nodes[-1])
                    if root_nodes
                    else substance_painter.layerstack.InsertPosition.from_textureset_stack(stack)
                )
                fill_layer = substance_painter.layerstack.insert_fill(position)
                fill_layer.set_name(layer_name)
            fill_layer.active_channels = {
                substance_painter.textureset.ChannelType.BaseColor
            }
            fill_layer.set_projection_mode(
                substance_painter.layerstack.ProjectionMode.UV
            )
            fill_layer.set_source(
                substance_painter.textureset.ChannelType.BaseColor,
                resource_id,
            )
            applied += 1
    _log(f"Applied Blender High Base Color to {applied} Painter Fill Layer(s)")


def _source_channel_spec(role):
    channel_names = {
        "BaseColor": ("BaseColor",),
        "AO": ("AO",),
        "ExtraR": ("AO",),
        "Roughness": ("Roughness", "SpecularRoughness"),
        "Metallic": ("Metallic", "BaseMetalness"),
    }.get(role, ())
    channel_type = next(
        (
            getattr(substance_painter.textureset.ChannelType, name)
            for name in channel_names
            if hasattr(substance_painter.textureset.ChannelType, name)
        ),
        None,
    )
    if channel_type is None:
        return None, None
    format_name = "sRGB8" if role == "BaseColor" else "L8"
    channel_format = getattr(
        substance_painter.textureset.ChannelFormat,
        format_name,
        None,
    )
    return channel_type, channel_format


def _usable_source_material_plan(texture_set_name, plan):
    if plan.get("Extra"):
        _log(
            f"Texture Set '{texture_set_name}': raw RGB Extra was not assigned to a "
            "grayscale channel; provide split ExtraR, Roughness, and Metallic maps"
        )
    usable = {}
    for role in ("BaseColor", "AO", "ExtraR", "Roughness", "Metallic"):
        image_path = plan.get(role)
        if not image_path:
            continue
        if not Path(image_path).is_file():
            _log(
                f"Texture Set '{texture_set_name}': source {role} map was not found: "
                f"{image_path}"
            )
            continue
        usable[role] = str(Path(image_path))
    if "AO" in usable and "ExtraR" in usable:
        _log(
            f"Texture Set '{texture_set_name}': explicit AO takes precedence over "
            "the Extra.R preservation transport"
        )
        usable.pop("ExtraR", None)
    elif "ExtraR" in usable:
        _log(
            f"Texture Set '{texture_set_name}': routing Extra.R to Painter's AO "
            "channel only as packed-channel preservation transport, not as an AO "
            "semantic claim"
        )
    return usable


def _managed_source_layer_name(request, texture_set_name, source_digest):
    asset_id = (
        request.get("asset_id")
        or request.get("asset_name")
        or request.get("asset")
        or texture_set_name
    )
    asset_token = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in str(asset_id)
    ).strip("_")
    return f"{MANAGED_SOURCE_LAYER_PREFIX} {asset_token} {source_digest[:12]}"


def _managed_source_nodes(root_nodes):
    return [
        node
        for node in root_nodes
        if isinstance(node, substance_painter.layerstack.FillLayerNode)
        and node.get_name().startswith(MANAGED_SOURCE_LAYER_PREFIX)
    ]


def _legacy_base_color_nodes(root_nodes):
    return [
        node
        for node in root_nodes
        if isinstance(node, substance_painter.layerstack.FillLayerNode)
        and node.get_name() == "Blender High Base Color"
    ]


def _resource_identity_text(resource_id):
    """Return stable, inspectable text for a Painter ResourceID.

    Painter versions expose slightly different ResourceID surfaces.  This
    intentionally fails closed when none of them reveal the managed resource
    name: an unverifiable layer is refreshed instead of being treated as a
    no-op.
    """
    values = []
    for attribute in ("name", "url", "identifier"):
        value = getattr(resource_id, attribute, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value not in (None, ""):
            values.append(str(value))
    try:
        values.append(str(resource_id))
    except Exception:
        pass
    return "\n".join(values)


def _managed_layer_source(layer, channel_type):
    getter = getattr(layer, "get_source", None)
    if callable(getter):
        try:
            return getter(channel_type)
        except Exception:
            return None
    sources = getattr(layer, "sources", None)
    if isinstance(sources, dict):
        return sources.get(channel_type)
    return None


def _managed_layer_projection(layer):
    getter = getattr(layer, "get_projection_mode", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return getattr(layer, "projection", None)


def _managed_layer_is_current(root_nodes, desired_name, expected_sources):
    managed = _managed_source_nodes(root_nodes)
    if not (
        len(managed) == 1
        and not _legacy_base_color_nodes(root_nodes)
        and bool(root_nodes)
        and managed[0] == root_nodes[-1]
        and managed[0].get_name() == desired_name
    ):
        return False
    layer = managed[0]
    if _managed_layer_projection(layer) != substance_painter.layerstack.ProjectionMode.UV:
        return False
    try:
        active_channels = set(layer.active_channels)
    except Exception:
        return False
    if active_channels != set(expected_sources):
        return False
    for channel_type, expected_source in expected_sources.items():
        actual_source = _managed_layer_source(layer, channel_type)
        if actual_source is None:
            return False
        if isinstance(expected_source, str):
            if expected_source not in _resource_identity_text(actual_source):
                return False
        elif actual_source != expected_source:
            return False
    return True


def _ensure_source_channel(stack, texture_set_name, role):
    channel_type, channel_format = _source_channel_spec(role)
    if channel_type is None:
        _log(
            f"Texture Set '{texture_set_name}': Painter has no compatible "
            f"channel type for source {role}"
        )
        return None
    try:
        if not stack.has_channel(channel_type):
            if channel_format is None:
                _log(
                    f"Texture Set '{texture_set_name}': Painter has no compatible "
                    f"channel format for source {role}"
                )
                return None
            stack.add_channel(channel_type, channel_format)
            _log(f"Texture Set '{texture_set_name}': added {role} channel")
        return channel_type
    except Exception as error:
        _log(
            f"Texture Set '{texture_set_name}': could not enable source {role} "
            f"channel: {error}"
        )
        return None


def _source_resource_name(texture_set_name, role, source_digest):
    texture_set_name = _canonical_painter_set_name(texture_set_name)
    resource_token = "".join(
        char if char.isalnum() else "_"
        for char in texture_set_name
    ).strip("_")
    resource_role = "ExtraR" if role == "ExtraR" else role
    return f"ST_{resource_token}_Source_{resource_role}_{source_digest[:12]}"


def _import_source_material_resources(texture_set_name, plan, source_digest):
    imported = {}
    for role, image_path in plan.items():
        try:
            resource = substance_painter.resource.import_project_resource(
                image_path,
                substance_painter.resource.Usage.TEXTURE,
                name=_source_resource_name(texture_set_name, role, source_digest),
                group=MANAGED_SOURCE_RESOURCE_GROUP,
            )
            imported[role] = resource.identifier()
        except Exception as error:
            _log(
                f"Texture Set '{texture_set_name}': could not import source "
                f"{role} map: {error}"
            )
    missing_roles = sorted(set(plan) - set(imported))
    if missing_roles:
        raise RuntimeError(
            f"Texture Set '{texture_set_name}': source resources were not "
            f"imported for {', '.join(missing_roles)}"
        )
    return imported


def _put_managed_source_layer_at_bottom(
    stack,
    texture_set_name,
    desired_name,
    channel_resources,
):
    root_nodes = substance_painter.layerstack.get_root_layer_nodes(stack)
    if _managed_layer_is_current(root_nodes, desired_name, channel_resources):
        return "reused", root_nodes[-1]

    managed = _managed_source_nodes(root_nodes)
    legacy = _legacy_base_color_nodes(root_nodes)
    clean_bottom = (
        len(managed) == 1
        and not legacy
        and managed[0] == root_nodes[-1]
    )
    if clean_bottom:
        fill_layer = managed[0]
    else:
        for node in managed + legacy:
            substance_painter.layerstack.delete_node(node)
        root_nodes = substance_painter.layerstack.get_root_layer_nodes(stack)
        position = (
            substance_painter.layerstack.InsertPosition.below_node(root_nodes[-1])
            if root_nodes
            else substance_painter.layerstack.InsertPosition.from_textureset_stack(stack)
        )
        fill_layer = substance_painter.layerstack.insert_fill(position)

    fill_layer.set_name(desired_name)
    fill_layer.active_channels = set(channel_resources)
    fill_layer.set_projection_mode(substance_painter.layerstack.ProjectionMode.UV)
    for channel_type, resource_id in channel_resources.items():
        fill_layer.set_source(channel_type, resource_id)
    return ("updated" if clean_bottom else "created"), fill_layer


def _apply_source_material_layers(request):
    """Create one idempotent bottom source layer per applicable stack.

    The new contract owns Base Color and the three split Extra transports. A
    request without ``source_material_maps`` keeps the legacy Base Color path.
    """
    if "source_material_maps" not in request:
        _apply_base_color_layers(request)
        return

    source_maps = _normalize_source_material_maps(request.get("source_material_maps"))
    legacy_base_color_maps = request.get("base_color_maps") or {}
    if not isinstance(legacy_base_color_maps, dict):
        legacy_base_color_maps = {}
    for texture_set_name, image_path in legacy_base_color_maps.items():
        source_maps.setdefault(str(texture_set_name), {}).setdefault(
            "BaseColor",
            image_path,
        )

    summary = {
        "managed_layer_count": 0,
        "created": 0,
        "updated": 0,
        "reused": 0,
        "texture_sets": {},
    }
    computed_hashes = {}
    for texture_set in substance_painter.textureset.all_texture_sets():
        texture_set_name = _texture_set_name(texture_set)
        requested_plan = _entry_for_texture_set(source_maps, texture_set_name)
        if not requested_plan:
            continue
        plan = _usable_source_material_plan(texture_set_name, requested_plan)
        if not plan:
            continue
        source_digest = _source_plan_digest(plan)
        computed_hashes[texture_set_name] = source_digest
        desired_name = _managed_source_layer_name(
            request,
            texture_set_name,
            source_digest,
        )
        expected_sources = {}
        for role in plan:
            channel_type, _channel_format = _source_channel_spec(role)
            if channel_type is not None:
                expected_sources[channel_type] = _source_resource_name(
                    texture_set_name,
                    role,
                    source_digest,
                )
        stacks = texture_set.all_stacks()
        current = [
            _managed_layer_is_current(
                substance_painter.layerstack.get_root_layer_nodes(stack),
                desired_name,
                expected_sources,
            )
            for stack in stacks
        ]
        if stacks and all(current):
            summary["reused"] += len(stacks)
            summary["managed_layer_count"] += len(stacks)
            summary["texture_sets"][texture_set_name] = {
                "channels": sorted(plan),
                "digest": source_digest,
                "layers": len(stacks),
                "result": "reused",
            }
            continue

        imported = _import_source_material_resources(
            texture_set_name,
            plan,
            source_digest,
        )
        stack_results = []
        applied_channels = set()
        for stack in stacks:
            channel_resources = {}
            for role, resource_id in imported.items():
                channel_type = _ensure_source_channel(stack, texture_set_name, role)
                if channel_type is not None:
                    channel_resources[channel_type] = resource_id
                    applied_channels.add(role)
            if len(channel_resources) != len(imported):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}': not every requested "
                    "source channel could be enabled"
                )
            result, _fill_layer = _put_managed_source_layer_at_bottom(
                stack,
                texture_set_name,
                desired_name,
                channel_resources,
            )
            stack_results.append(result)
            summary[result] += 1
            summary["managed_layer_count"] += 1
        if len(stack_results) != len(stacks):
            raise RuntimeError(
                f"Texture Set '{texture_set_name}': expected {len(stacks)} "
                f"managed source layer(s), applied {len(stack_results)}"
            )
        summary["texture_sets"][texture_set_name] = {
            "channels": sorted(applied_channels),
            "digest": source_digest,
            "layers": len(stack_results),
            "result": sorted(set(stack_results)),
        }

    if computed_hashes and not request.get("source_material_hashes"):
        request["source_material_hashes"] = computed_hashes
    request["source_layer_result"] = summary
    _log(
        "Managed Blender source layers: "
        f"{summary['managed_layer_count']} total, {summary['created']} created, "
        f"{summary['updated']} updated, {summary['reused']} unchanged"
    )


def _canonical_painter_set_name(name):
    value = str(name)
    return value[2:] if value.startswith("M_") else value


def _audit_expected_source_state(expected):
    """Fail closed if managed Painter sources changed before export."""
    if not isinstance(expected, dict) or expected.get("contract") != "meshy-source-state-v1":
        raise RuntimeError("Meshy export source-state contract is missing or invalid")
    canonical_sets = expected.get("canonical_texture_sets")
    if (
        not isinstance(canonical_sets, list)
        or canonical_sets != sorted(set(canonical_sets))
        or any(
            not value
            or any(not (char.isalnum() or char == "_") for char in value)
            for value in canonical_sets
        )
    ):
        raise RuntimeError("Meshy export canonical Texture Set IDs are invalid")
    painter_sets = substance_painter.textureset.all_texture_sets()
    by_name = {}
    for texture_set in painter_sets:
        name = _canonical_painter_set_name(_texture_set_name(texture_set))
        if name in by_name:
            raise RuntimeError(f"Painter has duplicate canonical Texture Set '{name}'")
        by_name[name] = texture_set
    if set(by_name) != set(canonical_sets):
        raise RuntimeError(
            "Painter Texture Sets changed after source setup: "
            f"Painter={sorted(by_name)}, expected={canonical_sets}"
        )

    expected_material = expected.get("material") or {}
    expected_normal = expected.get("normal") or {}
    if not isinstance(expected_material, dict) or not isinstance(expected_normal, dict):
        raise RuntimeError("Meshy export source-state entries must be dictionaries")
    if not set(expected_material).issubset(by_name) or not set(expected_normal).issubset(by_name):
        raise RuntimeError("Meshy source-state plan names an unknown Texture Set")

    for texture_set_name, texture_set in by_name.items():
        material_entry = expected_material.get(texture_set_name)
        for stack in texture_set.all_stacks():
            roots = substance_painter.layerstack.get_root_layer_nodes(stack)
            if material_entry is None:
                if _managed_source_nodes(roots) or _legacy_base_color_nodes(roots):
                    raise RuntimeError(
                        f"Texture Set '{texture_set_name}' has an unexpected source layer"
                    )
                continue
            channels = material_entry.get("channels")
            digest = str(material_entry.get("digest") or "")
            if not isinstance(channels, list) or channels != sorted(set(channels)):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' source channels are invalid"
                )
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' source digest is invalid"
                )
            managed = _managed_source_nodes(roots)
            if len(managed) != 1 or not managed[0].get_name().endswith(digest[:12]):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' managed source layer changed"
                )
            expected_sources = {}
            for role in channels:
                channel_type, _channel_format = _source_channel_spec(role)
                if channel_type is None:
                    raise RuntimeError(
                        f"Texture Set '{texture_set_name}' source channel {role} is unavailable"
                    )
                expected_sources[channel_type] = _source_resource_name(
                    texture_set_name,
                    role,
                    digest,
                )
            if not _managed_layer_is_current(
                roots,
                managed[0].get_name(),
                expected_sources,
            ):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' managed source state changed"
                )

        normal_entry = expected_normal.get(texture_set_name)
        if normal_entry is not None:
            source_sha256 = str(normal_entry.get("source_sha256") or "")
            resource_name = str(normal_entry.get("resource_name") or "")
            if (
                len(source_sha256) != 64
                or source_sha256[:12] not in resource_name
            ):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' source Normal pin is invalid"
                )
            normal_usage = substance_painter.textureset.MeshMapUsage.Normal
            try:
                resource_id = texture_set.get_mesh_map_resource(normal_usage)
            except Exception as error:
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' source Normal is unavailable: {error}"
                ) from error
            if resource_name not in _resource_identity_text(resource_id):
                raise RuntimeError(
                    f"Texture Set '{texture_set_name}' source Normal resource changed"
                )

    return {
        "contract": "meshy-source-state-v1",
        "canonical_texture_sets": list(canonical_sets),
        "material": json.loads(json.dumps(expected_material, sort_keys=True)),
        "normal": json.loads(json.dumps(expected_normal, sort_keys=True)),
        "exact": True,
    }


def _apply_alpha_color_layers(request):
    alpha_color_maps = request.get("alpha_color_maps", {})
    if not alpha_color_maps:
        return

    applied = 0
    for texture_set in substance_painter.textureset.all_texture_sets():
        texture_set_name = texture_set.name
        image_path = alpha_color_maps.get(texture_set_name)
        if not image_path or not Path(image_path).is_file():
            continue
        color_resource = substance_painter.resource.import_project_resource(
            image_path,
            substance_painter.resource.Usage.TEXTURE,
            name=f"{texture_set_name}_BlenderAlphaColor",
            group="Substance Tools",
        )
        mask_resource = substance_painter.resource.import_project_resource(
            image_path,
            substance_painter.resource.Usage.ALPHA,
            name=f"{texture_set_name}_BlenderAlphaMask",
            group="Substance Tools",
        )
        for stack in texture_set.all_stacks():
            root_nodes = substance_painter.layerstack.get_root_layer_nodes(stack)
            layer_name = "Blender Alpha Details"
            fill_layer = next(
                (
                    node
                    for node in root_nodes
                    if isinstance(node, substance_painter.layerstack.FillLayerNode)
                    and node.get_name() == layer_name
                ),
                None,
            )
            if fill_layer is None:
                fill_layer = substance_painter.layerstack.insert_fill(
                    substance_painter.layerstack.InsertPosition.from_textureset_stack(
                        stack
                    )
                )
                fill_layer.set_name(layer_name)
            fill_layer.active_channels = {
                substance_painter.textureset.ChannelType.BaseColor
            }
            fill_layer.set_projection_mode(
                substance_painter.layerstack.ProjectionMode.UV
            )
            fill_layer.set_source(
                substance_painter.textureset.ChannelType.BaseColor,
                color_resource.identifier(),
            )
            if not fill_layer.has_mask():
                fill_layer.add_mask(
                    substance_painter.layerstack.MaskBackground.Black
                )
            mask_fill = next(
                (
                    effect
                    for effect in fill_layer.mask_effects()
                    if isinstance(
                        effect,
                        substance_painter.layerstack.FillEffectNode,
                    )
                    and effect.get_name() == "Blender Alpha Mask"
                ),
                None,
            )
            if mask_fill is None:
                mask_fill = substance_painter.layerstack.insert_fill(
                    substance_painter.layerstack.InsertPosition.inside_node(
                        fill_layer,
                        substance_painter.layerstack.NodeStack.Mask,
                    )
                )
                mask_fill.set_name("Blender Alpha Mask")
            mask_fill.set_source(None, mask_resource.identifier())
            mask_fill.set_projection_mode(
                substance_painter.layerstack.ProjectionMode.UV
            )
            applied += 1
    _log(f"Applied Blender Alpha Details to {applied} Painter Fill Layer(s)")


def _schedule_successful_save_retry(request, reason, delay_ms=1000, max_retries=120):
    retry_count = int(request.get("_save_retry_count", 0))
    if retry_count >= max_retries:
        _log(f"Could not save the successful bake state after waiting: {reason}")
        return False
    request["_save_retry_count"] = retry_count + 1
    _single_shot_guarded(delay_ms, _save_successful_request, request)
    _log_timing(
        f"save wait {request['_save_retry_count']} scheduled after "
        f"{delay_ms} ms ({reason})"
    )
    return True


def _save_successful_request():
    global _processing, _active_request
    request = _active_request
    if request is None:
        _processing = False
        return
    try:
        if substance_painter.project.is_busy():
            if _schedule_successful_save_retry(request, "Painter is busy"):
                return
    except Exception as error:
        _log(f"Could not query Painter busy state before save: {error}")

    saved = False
    started = time.perf_counter()
    try:
        _require_requested_project_open(request, "finalize the successful bake")
        _normalize_texture_set_names()
        _apply_source_material_layers(request)
        _apply_alpha_color_layers(request)
        metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
        for key in (
            "pipeline_hash",
            "low_hash",
            "high_hash",
            "settings_hash",
            "base_color_hashes",
            "alpha_color_hashes",
            "back_normal_hashes",
        ):
            metadata.set(key, request.get(key, ""))
        for key in SOURCE_METADATA_KEYS:
            if key in request:
                metadata.set(key, request[key])

        _require_requested_project_open(request, "save the successful bake")
        project_path = substance_painter.project.file_path()
        requested_path = request["spp"]
        if project_path and (
            _normalized_path(project_path) == _normalized_path(requested_path)
        ):
            substance_painter.project.save()
        else:
            if not _is_new_create_request(request):
                raise RuntimeError(
                    "Refusing to save an existing-project request over a different .spp"
                )
            template_path = request.get("template")
            if template_path and (
                _normalized_path(template_path) == _normalized_path(requested_path)
            ):
                raise RuntimeError("Refusing to use the Painter template as the .spp target")
            if Path(requested_path).exists():
                raise RuntimeError(
                    f"Refusing to overwrite an unexpected existing Painter project: {requested_path}"
                )
            substance_painter.project.save_as(requested_path)
        saved = True
        if not _mark_request_success(request):
            raise RuntimeError(
                "Painter project saved, but the durable request receipt could not be updated"
            )
        _delete_claimed_pending_request(request)
        _log(f"Bake succeeded and project was saved: {requested_path}")
        _log_timing(f"post-bake layer update/save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Could not save the successful bake state: {error}")
        if _schedule_successful_save_retry(request, str(error)):
            return
    try:
        if saved:
            # Painter's Python API calls Painting mode "Edition".
            substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
            _log("Returned to Painting mode")
    except Exception as error:
        _log(f"Project was saved, but could not return to Painting mode: {error}")
    _active_request = None
    _processing = False


def _save_reimported_request():
    global _processing, _active_request, _last_polled_pipeline_hash
    request = _active_request
    if request is None:
        _processing = False
        return
    started = time.perf_counter()
    try:
        _require_requested_project_open(request, "apply the Painter update")
        _normalize_texture_set_names()
        _apply_source_material_layers(request)
        _apply_alpha_color_layers(request)
        metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
        for key in (
            "pipeline_hash",
            "low_hash",
            "high_hash",
            "settings_hash",
            "base_color_hashes",
            "alpha_color_hashes",
            "back_normal_hashes",
        ):
            metadata.set(key, request.get(key, ""))
        for key in SOURCE_METADATA_KEYS:
            if key in request:
                metadata.set(key, request[key])
        _require_requested_project_open(request, "save the Painter update")
        substance_painter.project.save()
        if not _mark_request_success(request):
            raise RuntimeError("Painter update saved, but its success receipt was not written")
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        if request.get("_low_reloaded"):
            _log("Low-poly mesh reimported without mesh-map baking; project saved")
        else:
            _log("Painter update applied without mesh-map baking; project saved")
        _log_timing(f"reload-only layer update/save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Low-poly mesh was reimported, but the update could not be saved: {error}")
        _last_polled_pipeline_hash = None
    _active_request = None
    _processing = False


def _save_normalized_request():
    global _processing, _active_request, _last_polled_pipeline_hash
    request = _active_request
    if request is None:
        _processing = False
        return
    started = time.perf_counter()
    try:
        _require_requested_project_open(request, "normalize Texture Set names")
        _normalize_texture_set_names()
        _require_requested_project_open(request, "save normalized Texture Set names")
        substance_painter.project.save()
        _mark_request_success(request)
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        _log("Texture Set names normalized and project saved")
        _log_timing(f"normalize/save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Could not normalize Texture Set names and save the project: {error}")
        if isinstance(error, _ProjectRequestMismatch):
            _last_polled_pipeline_hash = None
        else:
            _mark_request_failed(request, str(error))
    _active_request = None
    _processing = False


def _save_applied_maps_request():
    global _processing, _active_request, _last_polled_pipeline_hash
    request = _active_request
    if request is None:
        _processing = False
        return
    started = time.perf_counter()
    try:
        _require_requested_project_open(request, "apply source maps")
        _normalize_texture_set_names()
        _apply_source_material_layers(request)
        _apply_alpha_color_layers(request)
        _require_requested_project_open(request, "save applied source maps")
        substance_painter.project.save()
        _mark_request_success(request)
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        _log("Source material / Alpha maps applied and project saved")
        _log_timing(f"apply-maps save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Could not apply source material / Alpha maps: {error}")
        if isinstance(error, _ProjectRequestMismatch):
            _last_polled_pipeline_hash = None
        else:
            _mark_request_failed(request, str(error))
    _active_request = None
    _processing = False


def _on_baking_ended(event):
    global _processing, _active_request, _active_bake_callback
    callback = _active_bake_callback
    if callback is not None:
        substance_painter.event.DISPATCHER.disconnect(
            substance_painter.event.BakingProcessEnded,
            callback,
        )
        _active_bake_callback = None
    if _active_request is not None and _active_request.get("_bake_started_perf"):
        _log_timing(
            "baking process ended after "
            f"{(time.perf_counter() - _active_request['_bake_started_perf']) * 1000.0:.1f} ms"
        )
    if event.status == substance_painter.baking.BakingStatus.Success:
        request = _active_request
        _execute_when_not_busy_guarded(_save_successful_request, request)
    else:
        message = f"Baking did not complete successfully: {event.status}"
        _log(message)
        if _active_request is not None:
            _mark_request_failed(_active_request, message)
        _active_request = None
        _processing = False


def _single_rebake_texture_set(request):
    rebake_texture_sets = [
        str(name)
        for name in request.get("rebake_texture_sets", [])
        if str(name)
    ]
    if len(rebake_texture_sets) == 1:
        return rebake_texture_sets[0]
    return None


def _start_single_texture_set_bake(request, texture_set_name):
    bake_call_started = time.perf_counter()
    request["_bake_started_perf"] = bake_call_started
    substance_painter.js.evaluate(
        f"alg.baking.bake({json.dumps(texture_set_name)})"
    )
    _log_timing(
        f"alg.baking.bake({texture_set_name}) call took "
        f"{_elapsed_ms(bake_call_started):.1f} ms"
    )
    _log(f"Automatic single Texture Set mesh-map baking ran: {texture_set_name}")
    _single_shot_guarded(3000, _save_successful_request, request)


def _start_bake(request):
    global _processing, _active_request, _active_bake_callback
    try:
        _require_requested_project_open(request, "start mesh-map baking")
        started = time.perf_counter()
        renamed = _strip_texture_set_prefixes()
        request["texture_set_name_result"] = {
            "canonical": True,
            "renamed": [
                {"from": old_name, "to": new_name}
                for old_name, new_name in renamed
            ],
        }
        _configure_baking(request)
        _log_timing(f"_configure_baking returned after {_elapsed_ms(started):.1f} ms")
        single_texture_set = _single_rebake_texture_set(request)
        if single_texture_set:
            _start_single_texture_set_bake(request, single_texture_set)
            return

        bake_call_started = time.perf_counter()
        _active_bake_callback = _guard_async(_on_baking_ended, request)
        substance_painter.event.DISPATCHER.connect_strong(
            substance_painter.event.BakingProcessEnded,
            _active_bake_callback,
        )
        substance_painter.baking.bake_selected_textures_async()
        request["_bake_started_perf"] = time.perf_counter()
        _log_timing(f"bake_selected_textures_async call took {_elapsed_ms(bake_call_started):.1f} ms")
        _log("Automatic mesh-map baking started")
    except Exception as error:
        if _active_bake_callback is not None:
            try:
                substance_painter.event.DISPATCHER.disconnect(
                    substance_painter.event.BakingProcessEnded,
                    _active_bake_callback,
                )
            except Exception:
                pass
            _active_bake_callback = None
        message = f"Could not start automatic baking: {error}"
        _log(message)
        _mark_request_failed(request, message)
        _active_request = None
        _processing = False


def _after_reload(status):
    global _processing, _active_request, _last_polled_pipeline_hash
    reload_done_perf = time.perf_counter()
    if status != substance_painter.project.ReloadMeshStatus.SUCCESS:
        message = "Low-poly mesh reload failed; bake was not started"
        _log(message)
        if _active_request is not None:
            _mark_request_failed(_active_request, message)
        _active_request = None
        _processing = False
        return
    request = _active_request
    try:
        _require_requested_project_open(request, "continue after low-poly mesh reload")
    except _ProjectRequestMismatch as error:
        _log(str(error))
        _last_polled_pipeline_hash = None
        _active_request = None
        _processing = False
        return
    _log("Low-poly mesh reloaded")
    reload_started = request.get("_reload_started_perf")
    if reload_started:
        _log_timing(
            f"reload_mesh callback after {(reload_done_perf - reload_started) * 1000.0:.1f} ms"
        )
    request["_low_reloaded"] = True
    if request.get("_needs_bake"):
        _log("Bake-relevant data changed; baking after low-poly mesh reload")
        _start_bake(request)
    elif request.get("action") == "UPDATE":
        _execute_when_not_busy_guarded(_save_reimported_request, request)
    else:
        _start_bake(request)


def _after_reload_only(status):
    """Reload Mesh callback: persist the reloaded mesh.

    Existing Texture Sets keep their names: Painter matches the reloaded
    materials to existing Texture Sets by their imported (original) name, so a
    set that was already renamed on create stays renamed. Texture Sets newly
    introduced by the reload still carry the M_ prefix; that prefix is dropped
    in _save_reloaded_request, just before the project is saved.
    """
    global _processing, _active_request, _last_polled_pipeline_hash
    if status != substance_painter.project.ReloadMeshStatus.SUCCESS:
        message = "Low-poly mesh reload failed"
        _log(message)
        if _active_request is not None:
            _mark_request_failed(_active_request, message)
        _active_request = None
        _processing = False
        return
    request = _active_request
    try:
        _require_requested_project_open(request, "finish the Reload Mesh request")
    except _ProjectRequestMismatch as error:
        _log(str(error))
        _last_polled_pipeline_hash = None
        _active_request = None
        _processing = False
        return
    reload_started = request.get("_reload_started_perf")
    if reload_started:
        _log_timing(f"reload_mesh callback after {_elapsed_ms(reload_started):.1f} ms")
    _log("Low-poly mesh reloaded for Reload Mesh request")
    _execute_when_not_busy_guarded(_save_normalized_request, request)


def _resume_project_ready_when_idle(expected_generation):
    global _project_ready_idle_scheduled, _project_ready_idle_generation
    if (
        not _started
        or expected_generation != _plugin_generation
        or _project_ready_idle_generation != expected_generation
    ):
        return
    _project_ready_idle_scheduled = False
    _project_ready_idle_generation = None
    _on_project_ready()


def _on_project_ready(_event=None):
    global _processing, _active_request, _last_polled_pipeline_hash
    global _last_busy_log_time, _project_ready_idle_scheduled
    global _project_ready_idle_generation
    if not _started:
        return
    if _processing:
        return
    if substance_painter.project.is_busy():
        if not _project_ready_idle_scheduled:
            try:
                generation = _plugin_generation
                _project_ready_idle_scheduled = True
                _project_ready_idle_generation = generation
                substance_painter.project.execute_when_not_busy(
                    lambda generation=generation: _resume_project_ready_when_idle(
                        generation
                    )
                )
            except Exception as error:
                _project_ready_idle_scheduled = False
                _project_ready_idle_generation = None
                _log(f"Could not queue pending Painter request for idle state: {error}")
        now = time.perf_counter()
        if now - _last_busy_log_time >= 2.0:
            _log_timing("project is busy; request handling deferred")
            _last_busy_log_time = now
        return
    _project_ready_idle_scheduled = False
    _project_ready_idle_generation = None

    started = time.perf_counter()
    request = _load_request()
    if request is None:
        return
    request_marker = request.get("request_id") or request.get("pipeline_hash")
    if request_marker == _last_polled_pipeline_hash:
        return
    request["_accepted_perf"] = time.perf_counter()
    age = _request_age_ms(request)
    age_text = f", age={age:.1f} ms" if age is not None else ""
    loaded_perf = request.get("_loaded_perf")
    load_to_accept = (
        (request["_accepted_perf"] - loaded_perf) * 1000.0
        if loaded_perf
        else 0.0
    )
    _log_timing(
        f"request accepted in {_elapsed_ms(started):.1f} ms "
        f"(load_to_accept={load_to_accept:.1f} ms{age_text})"
    )
    if request.get("_loaded_from_pending") and not _claim_pending_request(request):
        _log(
            "Pending create request changed before acceptance; "
            "the replacement ticket will be handled on the next poll"
        )
        return

    if request.get("action") == "RELOAD_MESH":
        _processing = True
        _active_request = request
        _last_polled_pipeline_hash = request_marker
        reload_settings = substance_painter.project.MeshReloadingSettings(
            import_cameras=False,
            preserve_strokes=True,
        )
        try:
            request["_reload_started_perf"] = time.perf_counter()
            substance_painter.project.reload_mesh(
                request["low_fbx"],
                reload_settings,
                _guard_async(_after_reload_only, request),
            )
        except Exception as error:
            _processing = False
            _active_request = None
            _last_polled_pipeline_hash = None
            if "busy" in str(error).lower():
                _log("Painter is still loading; mesh reload will retry")
                return
            _log(f"Could not reload the low-poly mesh: {error}")
            _mark_request_failed(request, str(error))
        return

    if request.get("action") == "STRIP_PREFIX":
        _processing = True
        _active_request = request
        _last_polled_pipeline_hash = request_marker
        _execute_when_not_busy_guarded(_save_normalized_request, request)
        return

    if request.get("action") == "APPLY_MAPS":
        _processing = True
        _active_request = request
        _last_polled_pipeline_hash = request_marker
        _execute_when_not_busy_guarded(_save_applied_maps_request, request)
        return

    decision_started = time.perf_counter()
    metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
    strict_refresh = bool(
        request.get("strict_bake_settings") or request.get("expected_source_state")
    )
    if not strict_refresh and _request_matches_saved_metadata(metadata, request):
        _mark_request_success(request)
        _last_polled_pipeline_hash = request_marker
        _log("Existing Painter state already matches the request; startup bake skipped")
        return

    low_changed = (
        bool(request.get("low_changed"))
        if "low_changed" in request
        else _metadata_value(metadata, "low_hash") != _request_value(request, "low_hash")
    )
    changed_high_texture_sets = [
        str(name) for name in request.get("changed_high_texture_sets", [])
    ]
    high_changed = (
        bool(changed_high_texture_sets)
        if "changed_high_texture_sets" in request
        else _metadata_value(metadata, "high_hash")
        != _request_value(request, "high_hash")
    )
    settings_changed = (
        bool(request.get("settings_changed"))
        if "settings_changed" in request
        else _metadata_value(metadata, "settings_hash")
        != _request_value(request, "settings_hash")
    )
    source_material_changed = (
        bool(request.get("source_material_changed"))
        if "source_material_changed" in request
        else "source_material_maps" in request
        and (
            not request.get("source_material_hashes")
            or _metadata_value(metadata, "source_material_hashes")
            != _request_value(request, "source_material_hashes")
        )
    )
    source_normal_hash_keys = (
        "source_normal_mesh_hashes",
        "source_normal_hashes",
        "source_normal_mesh_map_hashes",
    )
    if "source_normal_mesh_changed" in request:
        source_normal_changed = bool(request.get("source_normal_mesh_changed"))
    elif "source_normal_changed" in request:
        source_normal_changed = bool(request.get("source_normal_changed"))
    else:
        source_normal_changed = (
            "source_normal_mesh_maps" in request
            and (
                not any(request.get(key) for key in source_normal_hash_keys)
                or any(
                    key in request
                    and _metadata_value(metadata, key) != _request_value(request, key)
                    for key in source_normal_hash_keys
                )
            )
        )
    rebake_texture_sets = [str(name) for name in request.get("rebake_texture_sets", [])]
    explicit_bake_plan = "rebake_texture_sets" in request
    needs_bake = bool(rebake_texture_sets) if explicit_bake_plan else (
        high_changed or settings_changed
    )
    needs_bake = needs_bake or source_normal_changed
    base_color_changed = bool(request.get("base_color_changed"))
    alpha_color_changed = bool(request.get("alpha_color_changed"))
    _log_timing(
        "change decision "
        f"low_changed={low_changed}, high_changed={high_changed}, "
        f"settings_changed={settings_changed}, "
        f"source_material_changed={source_material_changed}, "
        f"source_normal_changed={source_normal_changed}, needs_bake={needs_bake}, "
        f"rebake={rebake_texture_sets}, took={_elapsed_ms(decision_started):.1f} ms"
    )
    if (
        not strict_refresh
        and request.get("action") != "UPDATE"
        and metadata.get("pipeline_hash") == request.get("pipeline_hash")
        and not source_material_changed
        and not source_normal_changed
    ):
        _log("Mesh and bake settings are unchanged; reimport and baking skipped")
        _last_polled_pipeline_hash = request_marker
        return
    if (
        not strict_refresh
        and request.get("action") == "UPDATE"
        and explicit_bake_plan
        and not low_changed
        and not needs_bake
        and not base_color_changed
        and not alpha_color_changed
        and not source_material_changed
        and not source_normal_changed
    ):
        _log("No Painter work required; update request skipped")
        _last_polled_pipeline_hash = request_marker
        return

    _processing = True
    _active_request = request
    _active_request["_needs_bake"] = needs_bake
    _active_request["_low_reloaded"] = False
    _last_polled_pipeline_hash = request_marker
    must_reload = low_changed and (
        request.get("action") == "UPDATE" or bool(request.get("spp_existed"))
    )
    if must_reload:
        if needs_bake:
            _log("Low-poly mesh changed; reloading it before mesh-map baking")
        else:
            _log("Low-poly mesh changed; reimporting without mesh-map baking")
        reload_settings = substance_painter.project.MeshReloadingSettings(
            import_cameras=False,
            preserve_strokes=True,
        )
        try:
            reload_submit_started = time.perf_counter()
            request["_reload_started_perf"] = reload_submit_started
            substance_painter.project.reload_mesh(
                request["low_fbx"],
                reload_settings,
                _guard_async(_after_reload, request),
            )
            _log_timing(
                f"reload_mesh submit took {_elapsed_ms(reload_submit_started):.1f} ms"
            )
        except Exception as error:
            _processing = False
            _active_request = None
            _last_polled_pipeline_hash = None
            if "busy" in str(error).lower():
                _log("Painter is still loading; mesh reimport will retry")
                return
            raise
    elif needs_bake:
        if high_changed:
            _log("High-poly mesh changed; starting mesh-map baking")
        elif settings_changed:
            _log("Bake settings changed; starting mesh-map baking")
        _start_bake(request)
    elif request.get("action") == "UPDATE":
        _execute_when_not_busy_guarded(_save_reimported_request, request)
    else:
        _start_bake(request)


def start_plugin():
    global _started, _pending_timer, _startup_resources_ready, _plugin_generation
    global _active_bake_callback
    if _started:
        return
    if _active_bake_callback is not None:
        try:
            substance_painter.event.DISPATCHER.disconnect(
                substance_painter.event.BakingProcessEnded,
                _active_bake_callback,
            )
        except Exception:
            pass
        _active_bake_callback = None
    _plugin_generation += 1
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectEditionEntered,
        _on_project_ready,
    )
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ShelfCrawlingEnded,
        _mark_startup_resources_ready,
    )
    _pending_timer = QtCore.QTimer()
    _pending_timer.setInterval(500)
    _pending_timer.timeout.connect(_poll_requests)
    _pending_timer.start()
    _started = True
    if (
        substance_painter.project.is_open()
        and substance_painter.project.is_in_edition_state()
    ):
        _startup_resources_ready = True
        _on_project_ready()
        _single_shot_guarded(0, _create_pending_project)
    # ShelfCrawlingEnded is the supported startup-ready signal. The delayed
    # idle check also covers reloading this plugin after crawling already ended.
    _single_shot_guarded(5000, _mark_startup_resources_ready)


def close_plugin():
    global _started, _processing, _active_request, _pending_timer
    global _last_polled_pipeline_hash, _active_bake_callback
    global _last_export_request_id, _export_processing
    global _pending_creation_request_id, _pending_creation_started_at
    global _startup_resources_ready, _pending_replacement_blocked_reason
    global _pending_request_wait_reason, _project_ready_idle_scheduled
    global _project_ready_idle_generation, _plugin_generation
    if not _started:
        return
    # Invalidate every captured callback before disconnecting event sources.
    _started = False
    _plugin_generation += 1
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectEditionEntered,
        _on_project_ready,
    )
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ShelfCrawlingEnded,
        _mark_startup_resources_ready,
    )
    if _active_bake_callback is not None:
        try:
            substance_painter.event.DISPATCHER.disconnect(
                substance_painter.event.BakingProcessEnded,
                _active_bake_callback,
            )
        except Exception:
            pass
        _active_bake_callback = None
    if _pending_timer is not None:
        _pending_timer.stop()
        _pending_timer.deleteLater()
        _pending_timer = None
    _last_polled_pipeline_hash = None
    _active_request = None
    _processing = False
    _last_export_request_id = None
    _export_processing = False
    _pending_creation_request_id = None
    _pending_creation_started_at = 0.0
    _startup_resources_ready = False
    _pending_replacement_blocked_reason = None
    _pending_request_wait_reason = None
    _project_ready_idle_scheduled = False
    _project_ready_idle_generation = None


if __name__ == "__main__":
    start_plugin()

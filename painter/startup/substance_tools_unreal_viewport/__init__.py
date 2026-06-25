"""Automate the Blender Baking -> Substance 3D Painter handoff."""

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
METADATA_CONTEXT = "SubstanceToolsBlender"
_started = False
_processing = False
_active_request = None
_pending_timer = None
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


def _pending_request_path():
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TEMP")
        or Path.home()
    )
    return base / "SubstanceTools" / PENDING_REQUEST_FILENAME


def _load_request():
    for path in _request_candidates():
        if not path.is_file():
            continue
        try:
            request = json.loads(path.read_text(encoding="utf-8-sig"))
            project_path = substance_painter.project.file_path()
            if project_path and request.get("spp"):
                if _normalized_path(project_path) != _normalized_path(request["spp"]):
                    continue
            if request.get("status") in {"FAILED", "SUCCESS"}:
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


def _write_json(path, value):
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _normalized_resource_name(name):
    return str(name or "").lower().replace(" ", "").replace("_", "")


def _export_preset_url(request):
    requested_name = request.get("preset", "Unreal_V2")
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

    preset_path = Path(request.get("preset_path", ""))
    if preset_path.is_file():
        return f"resource://your_assets/{preset_path.stem}"
    return f"resource://your_assets/{requested_name}"


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

    Called right before saving so the names that get persisted, exported, and
    matched against Blender's (already M_-stripped) Texture Set names are clean.
    """
    try:
        _strip_texture_set_prefixes()
    except Exception as error:
        _log(f"Could not normalize Painter Texture Set names: {error}")


def _mark_request_failed(request, message):
    request_path = request.get("_request_path")
    if not request_path:
        return
    try:
        saved = dict(request)
        saved.pop("_request_path", None)
        saved.pop("_loaded_perf", None)
        saved.pop("_accepted_perf", None)
        saved.pop("_reload_started_perf", None)
        saved.pop("_bake_started_perf", None)
        saved.pop("_needs_bake", None)
        saved.pop("_low_reloaded", None)
        saved.pop("_save_retry_count", None)
        saved["status"] = "FAILED"
        saved["failure"] = message
        _write_json(Path(request_path), saved)
    except Exception as error:
        _log(f"Could not mark request failed: {error}")


def _mark_request_success(request):
    request_path = request.get("_request_path")
    if not request_path:
        return
    try:
        saved = dict(request)
        saved.pop("_request_path", None)
        saved.pop("_loaded_perf", None)
        saved.pop("_accepted_perf", None)
        saved.pop("_reload_started_perf", None)
        saved.pop("_bake_started_perf", None)
        saved.pop("_needs_bake", None)
        saved.pop("_low_reloaded", None)
        saved.pop("_save_retry_count", None)
        saved["status"] = "SUCCESS"
        saved.pop("failure", None)
        _write_json(Path(request_path), saved)
    except Exception as error:
        _log(f"Could not mark request successful: {error}")


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
        preset_url = _export_preset_url(request)
        _normalize_texture_set_names()
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
        result = substance_painter.export.export_project_textures({
            "exportShaderParams": False,
            "exportPath": request["texture_dir"],
            "defaultExportPreset": preset_url,
            "exportList": export_list,
            "exportParameters": [{
                "parameters": {
                    "paddingAlgorithm": "infinite",
                }
            }],
        })
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


def _create_pending_project():
    if substance_painter.project.is_open():
        return
    request = _load_pending_request()
    if request is None or request.get("spp_existed"):
        return
    template_path = Path(request.get("template", ""))
    if not template_path.is_file():
        _log(f"Unreal Engine template was not found: {template_path}")
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
        substance_painter.project.create(
            request["low_fbx"],
            template_file_path=str(template_path),
            settings=settings,
        )
        # The M_ prefix is dropped later (in the save step, once the project is
        # in edition state). project.create() returns before the Texture Sets
        # exist, so renaming here would be a no-op.
        _pending_request_path().unlink(missing_ok=True)
        _log(f"Project created from Painter's Unreal Engine template: {request['spp']}")
    except Exception as error:
        _log(f"Could not create project from Unreal Engine template: {error}")


def _poll_requests():
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
    source_name = str(normal_plan.get("source_texture_set", ""))
    source_path = str(normal_plan.get("source_normal_texture", ""))
    resource_id = None

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
            imported = substance_painter.resource.import_project_resource(
                source_path,
                substance_painter.resource.Usage.TEXTURE,
                name=f"{texture_set_name}_SourceNormalMeshMap",
                group="Substance Tools",
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
        texture_set.set_mesh_map_resource(normal_usage, resource_id)
        _log(
            f"{texture_set_name}: using {source_name or source_path} "
            "as Normal mesh map"
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
    rebake_texture_sets = [str(name) for name in request.get("rebake_texture_sets", [])]
    high_entry_by_texture_set = {
        str(entry.get("texture_set", "")): entry
        for entry in request.get("high_entries", [])
        if entry.get("texture_set")
    }
    high_path = request.get("high_fbx", "")

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
            continue

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
        normal_plan = next(
            (
                plan for name, plan in back_normal_mesh_maps.items()
                if _texture_set_matches(texture_set_name, [name])
            ),
            None,
        )
        if normal_plan and _assign_back_normal_mesh_map(texture_set, normal_plan):
            normal_usage = substance_painter.textureset.MeshMapUsage.Normal
            texture_set_enabled_maps = [
                usage for usage in texture_set_enabled_maps
                if usage != normal_usage
            ]
        params.set_enabled_bakers(texture_set_enabled_maps)
        common = params.common()
        changes = {}

        if "OutputSize" in common:
            exponent = int(math.log2(resolution))
            changes[common["OutputSize"]] = (exponent, exponent)
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

        substance_painter.baking.BakingParameters.set(changes)
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
    QtCore.QTimer.singleShot(delay_ms, _save_successful_request)
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
        _normalize_texture_set_names()
        _apply_base_color_layers(request)
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

        project_path = substance_painter.project.file_path()
        requested_path = request["spp"]
        if project_path:
            substance_painter.project.save()
        else:
            substance_painter.project.save_as(requested_path)
        saved = True
        _mark_request_success(request)
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
    global _processing, _active_request
    request = _active_request
    if request is None:
        _processing = False
        return
    started = time.perf_counter()
    try:
        _normalize_texture_set_names()
        _apply_base_color_layers(request)
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
        substance_painter.project.save()
        _mark_request_success(request)
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        if request.get("_low_reloaded"):
            _log("Low-poly mesh reimported without mesh-map baking; project saved")
        else:
            _log("Painter update applied without mesh-map baking; project saved")
        _log_timing(f"reload-only layer update/save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Low-poly mesh was reimported, but the update could not be saved: {error}")
    _active_request = None
    _processing = False


def _save_normalized_request():
    global _processing, _active_request
    request = _active_request
    if request is None:
        _processing = False
        return
    started = time.perf_counter()
    try:
        _normalize_texture_set_names()
        substance_painter.project.save()
        _mark_request_success(request)
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        _log("Texture Set names normalized and project saved")
        _log_timing(f"normalize/save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Could not normalize Texture Set names and save the project: {error}")
        _mark_request_failed(request, str(error))
    _active_request = None
    _processing = False


def _save_applied_maps_request():
    global _processing, _active_request
    request = _active_request
    if request is None:
        _processing = False
        return
    started = time.perf_counter()
    try:
        _normalize_texture_set_names()
        _apply_base_color_layers(request)
        _apply_alpha_color_layers(request)
        substance_painter.project.save()
        _mark_request_success(request)
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        _log("Base Color / Alpha maps applied and project saved")
        _log_timing(f"apply-maps save took {_elapsed_ms(started):.1f} ms")
    except Exception as error:
        _log(f"Could not apply Base Color / Alpha maps: {error}")
        _mark_request_failed(request, str(error))
    _active_request = None
    _processing = False


def _on_baking_ended(event):
    global _processing, _active_request
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.BakingProcessEnded,
        _on_baking_ended,
    )
    if _active_request is not None and _active_request.get("_bake_started_perf"):
        _log_timing(
            "baking process ended after "
            f"{(time.perf_counter() - _active_request['_bake_started_perf']) * 1000.0:.1f} ms"
        )
    if event.status == substance_painter.baking.BakingStatus.Success:
        substance_painter.project.execute_when_not_busy(_save_successful_request)
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
    QtCore.QTimer.singleShot(3000, _save_successful_request)


def _start_bake(request):
    global _processing, _active_request
    try:
        started = time.perf_counter()
        _configure_baking(request)
        _log_timing(f"_configure_baking returned after {_elapsed_ms(started):.1f} ms")
        single_texture_set = _single_rebake_texture_set(request)
        if single_texture_set:
            _start_single_texture_set_bake(request, single_texture_set)
            return

        bake_call_started = time.perf_counter()
        substance_painter.event.DISPATCHER.connect_strong(
            substance_painter.event.BakingProcessEnded,
            _on_baking_ended,
        )
        substance_painter.baking.bake_selected_textures_async()
        request["_bake_started_perf"] = time.perf_counter()
        _log_timing(f"bake_selected_textures_async call took {_elapsed_ms(bake_call_started):.1f} ms")
        _log("Automatic mesh-map baking started")
    except Exception as error:
        message = f"Could not start automatic baking: {error}"
        _log(message)
        _mark_request_failed(request, message)
        _active_request = None
        _processing = False


def _after_reload(status):
    global _processing, _active_request
    reload_done_perf = time.perf_counter()
    if status != substance_painter.project.ReloadMeshStatus.SUCCESS:
        message = "Low-poly mesh reload failed; bake was not started"
        _log(message)
        if _active_request is not None:
            _mark_request_failed(_active_request, message)
        _active_request = None
        _processing = False
        return
    _log("Low-poly mesh reloaded")
    reload_started = _active_request.get("_reload_started_perf") if _active_request else None
    if reload_started:
        _log_timing(
            f"reload_mesh callback after {(reload_done_perf - reload_started) * 1000.0:.1f} ms"
        )
    _active_request["_low_reloaded"] = True
    if _active_request.get("_needs_bake"):
        _log("Bake-relevant data changed; baking after low-poly mesh reload")
        _start_bake(_active_request)
    elif _active_request.get("action") == "UPDATE":
        substance_painter.project.execute_when_not_busy(_save_reimported_request)
    else:
        _start_bake(_active_request)


def _after_reload_only(status):
    """Reload Mesh callback: persist the reloaded mesh.

    Existing Texture Sets keep their names: Painter matches the reloaded
    materials to existing Texture Sets by their imported (original) name, so a
    set that was already renamed on create stays renamed. Texture Sets newly
    introduced by the reload still carry the M_ prefix; that prefix is dropped
    in _save_reloaded_request, just before the project is saved.
    """
    global _processing, _active_request
    if status != substance_painter.project.ReloadMeshStatus.SUCCESS:
        message = "Low-poly mesh reload failed"
        _log(message)
        if _active_request is not None:
            _mark_request_failed(_active_request, message)
        _active_request = None
        _processing = False
        return
    reload_started = (
        _active_request.get("_reload_started_perf") if _active_request else None
    )
    if reload_started:
        _log_timing(f"reload_mesh callback after {_elapsed_ms(reload_started):.1f} ms")
    _log("Low-poly mesh reloaded for Reload Mesh request")
    substance_painter.project.execute_when_not_busy(_save_normalized_request)


def _on_project_ready(_event=None):
    global _processing, _active_request, _last_polled_pipeline_hash
    global _last_busy_log_time
    if _processing:
        return
    if substance_painter.project.is_busy():
        now = time.perf_counter()
        if now - _last_busy_log_time >= 2.0:
            _log_timing("project is busy; request handling deferred")
            _last_busy_log_time = now
        return

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
                _after_reload_only,
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
        substance_painter.project.execute_when_not_busy(_save_normalized_request)
        return

    if request.get("action") == "APPLY_MAPS":
        _processing = True
        _active_request = request
        _last_polled_pipeline_hash = request_marker
        substance_painter.project.execute_when_not_busy(_save_applied_maps_request)
        return

    decision_started = time.perf_counter()
    metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
    if _request_matches_saved_metadata(metadata, request):
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
    rebake_texture_sets = [str(name) for name in request.get("rebake_texture_sets", [])]
    explicit_bake_plan = "rebake_texture_sets" in request
    needs_bake = bool(rebake_texture_sets) if explicit_bake_plan else (
        high_changed or settings_changed
    )
    base_color_changed = bool(request.get("base_color_changed"))
    alpha_color_changed = bool(request.get("alpha_color_changed"))
    _log_timing(
        "change decision "
        f"low_changed={low_changed}, high_changed={high_changed}, "
        f"settings_changed={settings_changed}, needs_bake={needs_bake}, "
        f"rebake={rebake_texture_sets}, took={_elapsed_ms(decision_started):.1f} ms"
    )
    if (
        request.get("action") != "UPDATE"
        and metadata.get("pipeline_hash") == request.get("pipeline_hash")
    ):
        _log("Mesh and bake settings are unchanged; reimport and baking skipped")
        _last_polled_pipeline_hash = request_marker
        return
    if (
        request.get("action") == "UPDATE"
        and explicit_bake_plan
        and not low_changed
        and not needs_bake
        and not base_color_changed
        and not alpha_color_changed
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
                _after_reload,
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
        substance_painter.project.execute_when_not_busy(_save_reimported_request)
    else:
        _start_bake(request)


def start_plugin():
    global _started, _pending_timer
    if _started:
        return
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectEditionEntered,
        _on_project_ready,
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
        _on_project_ready()
    else:
        QtCore.QTimer.singleShot(0, _create_pending_project)


def close_plugin():
    global _started, _pending_timer, _last_polled_pipeline_hash
    global _last_export_request_id, _export_processing
    if not _started:
        return
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectEditionEntered,
        _on_project_ready,
    )
    if _pending_timer is not None:
        _pending_timer.stop()
        _pending_timer.deleteLater()
        _pending_timer = None
    _last_polled_pipeline_hash = None
    _last_export_request_id = None
    _export_processing = False
    _started = False


if __name__ == "__main__":
    start_plugin()

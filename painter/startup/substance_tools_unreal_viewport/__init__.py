"""Automate the Blender Baking -> Substance 3D Painter handoff."""

import json
import math
import os
from pathlib import Path

from PySide6 import QtCore

import substance_painter.baking
import substance_painter.event
import substance_painter.export
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


def _normalized_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _log(message):
    print(f"[Substance Tools] {message}")


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
            request = json.loads(path.read_text(encoding="utf-8"))
            project_path = substance_painter.project.file_path()
            if project_path and request.get("spp"):
                if _normalized_path(project_path) != _normalized_path(request["spp"]):
                    continue
            return request
        except (OSError, ValueError) as error:
            _log(f"Could not read {path}: {error}")
    return None


def _load_pending_request():
    path = _pending_request_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
        request = json.loads(claimed_request_path.read_text(encoding="utf-8"))
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
        requested_name = request.get("preset", "Unreal_V2")
        normalized_name = requested_name.lower().replace(" ", "").replace("_", "")
        preset = next(
            (
                candidate
                for candidate in substance_painter.export.list_resource_export_presets()
                if candidate.resource_id.name.lower().replace(" ", "").replace("_", "")
                == normalized_name
            ),
            None,
        )
        if preset is None:
            raise RuntimeError(f"Painter export preset was not found: {requested_name}")
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
            "defaultExportPreset": preset.resource_id.url(),
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


def _enum_value_containing(prop, *needles):
    if prop is None:
        return None
    normalized_needles = tuple(needle.lower().replace(" ", "") for needle in needles)
    for label, value in prop.enum_values().items():
        normalized_label = label.lower().replace(" ", "")
        if all(needle in normalized_label for needle in normalized_needles):
            return value
    return None


def _mesh_map_usages(names):
    usages = []
    entries = substance_painter.textureset.MeshMapUsage.__entries
    for name in names:
        entry = entries.get(name)
        if entry:
            usages.append(entry[0])
    return usages


def _configure_baking(request):
    settings = request["settings"]
    resolution = int(settings.get("resolution", 2048))
    texture_sets = substance_painter.textureset.all_texture_sets()
    substance_painter.textureset.set_resolutions(
        texture_sets,
        substance_painter.textureset.Resolution(resolution, resolution),
    )

    enabled_maps = _mesh_map_usages(settings.get("mesh_maps", []))
    high_path = request.get("high_fbx", "")
    high_url = (
        QtCore.QUrl.fromLocalFile(str(Path(high_path).resolve())).toString()
        if high_path
        else ""
    )

    for texture_set in texture_sets:
        params = substance_painter.baking.BakingParameters.from_texture_set(texture_set)
        params.set_textureset_enabled(True)
        params.set_enabled_bakers(enabled_maps)
        common = params.common()
        changes = {}

        if "OutputSize" in common:
            exponent = int(math.log2(resolution))
            changes[common["OutputSize"]] = (exponent, exponent)
        if "LowAsHigh" in common:
            changes[common["LowAsHigh"]] = not bool(high_path)
        if high_path and "HipolyMesh" in common:
            changes[common["HipolyMesh"]] = high_url

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
        f"Configured {len(texture_sets)} Texture Set(s), "
        f"{resolution}px, match={settings.get('match')}"
    )


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


def _save_successful_request():
    global _processing, _active_request
    request = _active_request
    if request is None:
        _processing = False
        return
    saved = False
    try:
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
        ):
            metadata.set(key, request.get(key, ""))

        project_path = substance_painter.project.file_path()
        requested_path = request["spp"]
        if project_path:
            substance_painter.project.save()
        else:
            substance_painter.project.save_as(requested_path)
        saved = True
        _log(f"Bake succeeded and project was saved: {requested_path}")
    except Exception as error:
        _log(f"Could not save the successful bake state: {error}")
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
    try:
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
        ):
            metadata.set(key, request.get(key, ""))
        substance_painter.project.save()
        substance_painter.ui.switch_to_mode(substance_painter.ui.UIMode.Edition)
        _log("Low-poly mesh reimported without mesh-map baking; project saved")
    except Exception as error:
        _log(f"Low-poly mesh was reimported, but the update could not be saved: {error}")
    _active_request = None
    _processing = False


def _on_baking_ended(event):
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.BakingProcessEnded,
        _on_baking_ended,
    )
    if event.status == substance_painter.baking.BakingStatus.Success:
        substance_painter.project.execute_when_not_busy(_save_successful_request)
    else:
        global _processing, _active_request
        _log(f"Baking did not complete successfully: {event.status}")
        _active_request = None
        _processing = False


def _start_bake(request):
    try:
        _configure_baking(request)
        substance_painter.event.DISPATCHER.connect_strong(
            substance_painter.event.BakingProcessEnded,
            _on_baking_ended,
        )
        substance_painter.baking.bake_selected_textures_async()
        _log("Automatic mesh-map baking started")
    except Exception as error:
        global _processing, _active_request
        _log(f"Could not start automatic baking: {error}")
        _active_request = None
        _processing = False


def _after_reload(status):
    if status != substance_painter.project.ReloadMeshStatus.SUCCESS:
        global _processing, _active_request
        _log("Low-poly mesh reload failed; bake was not started")
        _active_request = None
        _processing = False
        return
    _log("Low-poly mesh reloaded")
    if _active_request.get("action") == "UPDATE":
        substance_painter.project.execute_when_not_busy(_save_reimported_request)
    else:
        _start_bake(_active_request)


def _on_project_ready(_event=None):
    global _processing, _active_request, _last_polled_pipeline_hash
    if _processing:
        return
    if substance_painter.project.is_busy():
        return

    request = _load_request()
    if request is None:
        return
    request_marker = request.get("request_id") or request.get("pipeline_hash")
    if request_marker == _last_polled_pipeline_hash:
        return

    metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
    if (
        request.get("action") != "UPDATE"
        and metadata.get("pipeline_hash") == request.get("pipeline_hash")
    ):
        _log("Mesh and bake settings are unchanged; reimport and baking skipped")
        _last_polled_pipeline_hash = request_marker
        return

    _processing = True
    _active_request = request
    _last_polled_pipeline_hash = request_marker
    must_reload = request.get("action") == "UPDATE" or (
        bool(request.get("spp_existed"))
        and metadata.get("low_hash") != request.get("low_hash")
    )
    if must_reload:
        if request.get("action") == "UPDATE":
            _log("Update requested; reimporting the low-poly mesh without baking")
        else:
            _log("Low-poly mesh changed; reloading it before baking")
        reload_settings = substance_painter.project.MeshReloadingSettings(
            import_cameras=False,
            preserve_strokes=True,
        )
        try:
            substance_painter.project.reload_mesh(
                request["low_fbx"],
                reload_settings,
                _after_reload,
            )
        except Exception as error:
            _processing = False
            _active_request = None
            _last_polled_pipeline_hash = None
            if "busy" in str(error).lower():
                _log("Painter is still loading; mesh reimport will retry")
                return
            raise
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

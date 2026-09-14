import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "painter"
    / "startup"
    / "substance_tools_unreal_viewport"
    / "__init__.py"
)


def _load_plugin_with_import_stubs():
    names = [
        "substance_painter",
        "substance_painter.baking",
        "substance_painter.event",
        "substance_painter.export",
        "substance_painter.js",
        "substance_painter.layerstack",
        "substance_painter.project",
        "substance_painter.resource",
        "substance_painter.source",
        "substance_painter.textureset",
        "substance_painter.ui",
        "PySide6",
        "PySide6.QtCore",
    ]
    original = {name: sys.modules.get(name) for name in names}
    try:
        painter = types.ModuleType("substance_painter")
        painter.__path__ = []
        sys.modules["substance_painter"] = painter
        for child in (
            "baking",
            "event",
            "export",
            "js",
            "layerstack",
            "project",
            "resource",
            "source",
            "textureset",
            "ui",
        ):
            module = types.ModuleType(f"substance_painter.{child}")
            setattr(painter, child, module)
            sys.modules[module.__name__] = module

        pyside = types.ModuleType("PySide6")
        pyside.__path__ = []
        qtcore = types.ModuleType("PySide6.QtCore")
        pyside.QtCore = qtcore
        sys.modules["PySide6"] = pyside
        sys.modules["PySide6.QtCore"] = qtcore

        spec = importlib.util.spec_from_file_location(
            "substance_tools_painter_source_map_test_target",
            PLUGIN_PATH,
        )
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        return plugin, painter
    finally:
        for name, module in original.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _FakeStack:
    def __init__(self):
        self.channels = set()

    def has_channel(self, channel):
        return channel in self.channels

    def add_channel(self, channel, _channel_format):
        self.channels.add(channel)


class _FakeFill:
    def __init__(self, name=""):
        self.name = name
        self.active_channels = set()
        self.sources = {}
        self.projection = None

    def get_name(self):
        return self.name

    def set_name(self, name):
        self.name = name

    def set_projection_mode(self, projection):
        self.projection = projection

    def get_projection_mode(self):
        return self.projection

    def set_source(self, channel, resource):
        self.sources[channel] = resource

    def get_source(self, channel):
        return self.sources.get(channel)


class _FakeTextureSet:
    def __init__(self, name, stack):
        self.name = name
        self._stacks = [stack]
        self.mesh_maps = {}

    def all_stacks(self):
        return list(self._stacks)

    def get_mesh_map_resource(self, usage):
        return self.mesh_maps.get(usage)

    def set_mesh_map_resource(self, usage, resource):
        self.mesh_maps[usage] = resource


class PainterSourceMapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin, cls.painter = _load_plugin_with_import_stubs()

    def test_normalizes_texture_first_role_first_and_list_contracts(self):
        normalize = self.plugin._normalize_source_material_maps
        self.assertEqual(
            normalize(
                {
                    "Cushion": {
                        "base_color": "color.png",
                        "extra_r": {"path": "extra_r.png"},
                        "roughness_path": "rough.png",
                        "Metalness": "metal.png",
                        "Extra": "packed.png",
                    }
                }
            ),
            {
                "Cushion": {
                    "BaseColor": "color.png",
                    "ExtraR": "extra_r.png",
                    "Roughness": "rough.png",
                    "Metallic": "metal.png",
                    "Extra": "packed.png",
                }
            },
        )
        self.assertEqual(
            normalize(
                {
                    "BaseColor": {"Cushion": "color.png"},
                    "Roughness": {"Cushion": "rough.png"},
                }
            ),
            {
                "Cushion": {
                    "BaseColor": "color.png",
                    "Roughness": "rough.png",
                }
            },
        )
        self.assertEqual(
            normalize(
                [
                    {
                        "texture_set": "Cushion",
                        "role": "metallic",
                        "path": "metal.png",
                    }
                ]
            ),
            {"Cushion": {"Metallic": "metal.png"}},
        )

    def test_normalizes_source_normal_mesh_map_contracts(self):
        normalize = self.plugin._normalize_source_normal_mesh_maps
        self.assertEqual(
            normalize({"Cushion": "normal.png"}),
            {"Cushion": {"source_normal_texture": "normal.png"}},
        )
        self.assertEqual(
            normalize({"Normal": {"Cushion": "normal.png"}}),
            {"Cushion": {"source_normal_texture": "normal.png"}},
        )
        self.assertEqual(
            normalize(
                [
                    {
                        "texture_set": "Cushion",
                        "path": "normal.png",
                        "basis": "low_tangent",
                    }
                ]
            ),
            {
                "Cushion": {
                    "texture_set": "Cushion",
                    "path": "normal.png",
                    "basis": "low_tangent",
                    "source_normal_texture": "normal.png",
                }
            },
        )

    def test_matching_duplicate_request_files_receive_one_terminal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "texture" / self.plugin.REQUEST_FILENAME
            second = root / "project" / self.plugin.REQUEST_FILENAME
            first.parent.mkdir()
            second.parent.mkdir()
            seed = {"request_id": "same-request", "status": "PENDING"}
            first.write_text(__import__("json").dumps(seed), encoding="utf-8")
            second.write_text(__import__("json").dumps(seed), encoding="utf-8")
            original_candidates = self.plugin._request_candidates
            self.plugin._request_candidates = lambda: [first, second]
            try:
                request = {
                    **seed,
                    "_request_path": str(first),
                    "source_layer_result": {"managed_layer_count": 1},
                }
                self.plugin._mark_request_success(request)
            finally:
                self.plugin._request_candidates = original_candidates
            for path in (first, second):
                saved = __import__("json").loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "SUCCESS")
                self.assertEqual(
                    saved["source_layer_result"],
                    {"managed_layer_count": 1},
                )

    def test_strict_meshy_requests_bypass_matching_metadata_fast_paths(self):
        plugin = self.plugin
        metadata = {
            "pipeline_hash": "matching-pipeline",
            "low_hash": "matching-low",
            "high_hash": "matching-high",
            "settings_hash": "matching-settings",
        }
        current_request = {}
        successes = []
        bakes = []
        logs = []

        def load_request():
            return current_request["value"]

        def run_request(marker, **strict_fields):
            request = {
                "request_id": marker,
                "pipeline_hash": metadata["pipeline_hash"],
                "low_hash": metadata["low_hash"],
                "high_hash": metadata["high_hash"],
                "settings_hash": metadata["settings_hash"],
                **strict_fields,
            }
            self.assertTrue(plugin._request_matches_saved_metadata(metadata, request))
            current_request["value"] = request
            plugin._processing = False
            plugin._active_request = None
            plugin._last_polled_pipeline_hash = None
            plugin._on_project_ready()
            return request

        with (
            mock.patch.object(self.painter.project, "is_busy", return_value=False, create=True),
            mock.patch.object(
                self.painter.project,
                "Metadata",
                return_value=metadata,
                create=True,
            ),
            mock.patch.object(plugin, "_load_request", side_effect=load_request),
            mock.patch.object(
                plugin,
                "_mark_request_success",
                side_effect=lambda request: successes.append(request["request_id"]),
            ),
            mock.patch.object(
                plugin,
                "_start_bake",
                side_effect=lambda request: bakes.append(request["request_id"]),
            ),
            mock.patch.object(plugin, "_log", side_effect=logs.append),
            mock.patch.object(plugin, "_log_timing"),
            mock.patch.object(plugin, "_processing", False),
            mock.patch.object(plugin, "_active_request", None),
            mock.patch.object(plugin, "_last_polled_pipeline_hash", None),
            mock.patch.object(plugin, "_started", True),
        ):
            run_request("legacy")
            self.assertEqual(successes, ["legacy"])
            self.assertEqual(bakes, [])
            self.assertIn(
                "Existing Painter state already matches the request; startup bake skipped",
                logs,
            )

            for marker, strict_fields in (
                ("strict-settings", {"strict_bake_settings": True}),
                (
                    "strict-source-state",
                    {"expected_source_state": {"contract": "meshy-source-state-v1"}},
                ),
            ):
                with self.subTest(marker=marker):
                    prior_successes = list(successes)
                    prior_logs = len(logs)
                    run_request(marker, **strict_fields)
                    self.assertEqual(successes, prior_successes)
                    self.assertEqual(bakes[-1], marker)
                    self.assertNotIn(
                        "Existing Painter state already matches the request; startup bake skipped",
                        logs[prior_logs:],
                    )

    def _install_fake_runtime(self, texture_set, roots):
        painter = self.painter

        class ChannelType:
            BaseColor = "base_color"
            AO = "ao"
            Roughness = "roughness"
            Metallic = "metallic"

        class ChannelFormat:
            sRGB8 = "srgb8"
            L8 = "l8"

        class MeshMapUsage:
            Normal = "normal"
            AO = "mesh_ao"
            ID = "id"

        setattr(MeshMapUsage, "__entries", {
            "Normal": (MeshMapUsage.Normal,),
            "AO": (MeshMapUsage.AO,),
        })

        painter.textureset.ChannelType = ChannelType
        painter.textureset.ChannelFormat = ChannelFormat
        painter.textureset.MeshMapUsage = MeshMapUsage
        painter.textureset.all_texture_sets = lambda: [texture_set]
        painter.textureset.Resolution = lambda width, height: (width, height)
        painter.textureset.set_resolutions = lambda _sets, _resolution: None

        class InsertPosition:
            @staticmethod
            def below_node(node):
                return ("below", node)

            @staticmethod
            def from_textureset_stack(stack):
                return ("stack", stack)

        painter.layerstack.FillLayerNode = _FakeFill
        painter.layerstack.InsertPosition = InsertPosition
        painter.layerstack.ProjectionMode = types.SimpleNamespace(UV="uv")
        painter.layerstack.get_root_layer_nodes = lambda stack: list(roots[stack])

        def insert_fill(position):
            layer = _FakeFill()
            if position[0] == "stack":
                stack = position[1]
                roots[stack].insert(0, layer)
            else:
                anchor = position[1]
                stack = next(stack for stack, nodes in roots.items() if anchor in nodes)
                nodes = roots[stack]
                nodes.insert(nodes.index(anchor) + 1, layer)
            return layer

        painter.layerstack.insert_fill = insert_fill
        painter.layerstack.delete_node = lambda node: next(
            nodes.remove(node) for nodes in roots.values() if node in nodes
        )

        self.imports = []

        class ResourceID:
            def __init__(self, name, version):
                self.name = name
                self.version = version

            def __eq__(self, other):
                return (
                    isinstance(other, ResourceID)
                    and self.name == other.name
                    and self.version == other.version
                )

        class Imported:
            def __init__(self, identifier):
                self._identifier = identifier

            def identifier(self):
                return self._identifier

        painter.resource.Usage = types.SimpleNamespace(TEXTURE="texture")

        def import_project_resource(path, _usage, name=None, group=None):
            identifier = ResourceID(name, f"{Path(path).name}:{len(self.imports)}")
            self.imports.append((path, name, group))
            return Imported(identifier)

        painter.resource.import_project_resource = import_project_resource

    def test_managed_layer_is_bottom_single_and_second_run_is_noop(self):
        stack = _FakeStack()
        texture_set = _FakeTextureSet("M_Cushion", stack)
        unmanaged = _FakeFill("Artist Paint")
        roots = {stack: [unmanaged]}
        self._install_fake_runtime(texture_set, roots)

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            paths = {}
            for role in ("BaseColor", "Extra", "ExtraR", "Roughness", "Metallic"):
                path = directory / f"{role}.png"
                path.write_bytes(role.encode("ascii"))
                paths[role] = str(path)
            request = {
                "asset_id": "Huya_Cushion",
                "source_material_maps": {"Cushion": paths},
            }

            self.plugin._apply_source_material_layers(request)
            self.assertEqual(len(self.imports), 4)
            self.assertEqual(len(roots[stack]), 2)
            managed = roots[stack][-1]
            self.assertTrue(
                managed.get_name().startswith(self.plugin.MANAGED_SOURCE_LAYER_PREFIX)
            )
            self.assertEqual(
                managed.active_channels,
                {"base_color", "ao", "roughness", "metallic"},
            )
            self.assertEqual(request["source_layer_result"]["created"], 1)

            self.plugin._apply_source_material_layers(request)
            self.assertEqual(len(self.imports), 4)
            self.assertEqual(len(roots[stack]), 2)
            self.assertEqual(request["source_layer_result"]["reused"], 1)

            managed.active_channels.remove("metallic")
            self.plugin._apply_source_material_layers(request)
            self.assertEqual(len(self.imports), 8)
            self.assertEqual(request["source_layer_result"]["updated"], 1)
            self.assertEqual(
                managed.active_channels,
                {"base_color", "ao", "roughness", "metallic"},
            )

            managed.projection = "planar"
            self.plugin._apply_source_material_layers(request)
            self.assertEqual(len(self.imports), 12)
            self.assertEqual(request["source_layer_result"]["updated"], 1)
            self.assertEqual(managed.projection, "uv")

            managed.sources["roughness"] = object()
            self.plugin._apply_source_material_layers(request)
            self.assertEqual(len(self.imports), 16)
            self.assertEqual(request["source_layer_result"]["updated"], 1)

            roots[stack].append(_FakeFill(managed.get_name()))
            self.plugin._apply_source_material_layers(request)
            managed_nodes = [
                node
                for node in roots[stack]
                if node.get_name().startswith(
                    self.plugin.MANAGED_SOURCE_LAYER_PREFIX
                )
            ]
            self.assertEqual(len(managed_nodes), 1)
            self.assertIs(managed_nodes[0], roots[stack][-1])

    def test_export_source_state_audit_rejects_layer_and_normal_swaps(self):
        stack = _FakeStack()
        texture_set = _FakeTextureSet("M_Cushion", stack)
        roots = {stack: [_FakeFill("Artist Paint")]}
        self._install_fake_runtime(texture_set, roots)

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            paths = {}
            for role in ("BaseColor", "ExtraR", "Roughness", "Metallic"):
                path = directory / f"{role}.png"
                path.write_bytes(role.encode("ascii"))
                paths[role] = str(path)
            request = {
                "asset_id": "Huya_Cushion",
                "source_material_maps": {"Cushion": paths},
            }
            self.plugin._apply_source_material_layers(request)
            layer_entry = request["source_layer_result"]["texture_sets"]["M_Cushion"]

            normal = directory / "Normal.png"
            normal.write_bytes(b"directx-low-tangent-normal")
            normal_sha = self.plugin._file_sha256(normal)
            normal_name = f"ST_Cushion_SourceNormal_{normal_sha[:12]}"
            texture_set.mesh_maps["normal"] = types.SimpleNamespace(name=normal_name)
            expected = {
                "contract": "meshy-source-state-v1",
                "canonical_texture_sets": ["Cushion"],
                "material": {
                    "Cushion": {
                        "channels": list(layer_entry["channels"]),
                        "digest": layer_entry["digest"],
                    }
                },
                "normal": {
                    "Cushion": {
                        "source_sha256": normal_sha,
                        "resource_name": normal_name,
                    }
                },
            }
            receipt = self.plugin._audit_expected_source_state(expected)
            self.assertTrue(receipt["exact"])

            managed = roots[stack][-1]
            saved_channels = set(managed.active_channels)
            managed.active_channels.remove("metallic")
            with self.assertRaisesRegex(RuntimeError, "source state changed"):
                self.plugin._audit_expected_source_state(expected)
            managed.active_channels = saved_channels

            saved_roughness = managed.sources["roughness"]
            managed.sources["roughness"] = object()
            with self.assertRaisesRegex(RuntimeError, "source state changed"):
                self.plugin._audit_expected_source_state(expected)
            managed.sources["roughness"] = saved_roughness

            texture_set.mesh_maps["normal"] = types.SimpleNamespace(name="swapped")
            with self.assertRaisesRegex(RuntimeError, "Normal resource changed"):
                self.plugin._audit_expected_source_state(expected)

    def test_source_normal_replaces_mesh_map_and_normal_baker_is_omitted(self):
        stack = _FakeStack()
        texture_set = _FakeTextureSet("M_Cushion", stack)
        roots = {stack: []}
        self._install_fake_runtime(texture_set, roots)

        class Parameters:
            instances = []

            def __init__(self):
                self.enabled_bakers = None
                self.enabled = None
                Parameters.instances.append(self)

            @classmethod
            def from_texture_set(cls, _texture_set):
                return cls()

            @staticmethod
            def set(_changes):
                return None

            def set_textureset_enabled(self, enabled):
                self.enabled = enabled

            def set_enabled_bakers(self, bakers):
                self.enabled_bakers = list(bakers)

            def common(self):
                return {}

            def baker(self, _usage):
                return {}

        self.painter.baking.BakingParameters = Parameters
        self.painter.baking.unlink_all_common_parameters = lambda: None

        with tempfile.TemporaryDirectory() as directory:
            normal = Path(directory) / "normal.png"
            normal.write_bytes(b"low-tangent-normal")
            expected_normal_sha256 = self.plugin._file_sha256(normal)
            request = {
                "settings": {
                    "resolution": 2048,
                    "mesh_maps": ["Normal", "AO"],
                    "match": "BY_MESH_NAME",
                    "antialiasing": "X2",
                    "id_source": "MATERIAL_COLOR",
                },
                "source_normal_mesh_maps": {
                    "Cushion": {
                        "source_normal_texture": str(normal),
                        "normal_convention": "DIRECTX",
                        "basis": "LOW_TANGENT",
                    }
                },
            }
            self.plugin._configure_baking(request)
            imported_count = len(self.imports)
            self.plugin._configure_baking(request)

        self.assertIn("normal", texture_set.mesh_maps)
        self.assertEqual(len(self.imports), imported_count)
        self.assertEqual(Parameters.instances[-1].enabled_bakers, ["mesh_ao"])
        result = request["source_normal_mesh_map_result"]
        self.assertEqual(result["assigned_count"], 1)
        self.assertEqual(result["normal_baker_omitted_count"], 1)
        assignment = result["assignments"]["M_Cushion"]
        self.assertEqual(
            assignment["source_sha256"],
            expected_normal_sha256,
        )
        self.assertIn(assignment["resource_name"], assignment["resource_identity"])

    def test_strict_meshy_bake_settings_resolve_and_report_exact_values(self):
        stack = _FakeStack()
        texture_set = _FakeTextureSet("M_Cushion", stack)
        self._install_fake_runtime(texture_set, {stack: []})

        class Property:
            def __init__(self, name, values):
                self.name = name
                self.values = values

            def short_name(self):
                return self.name

            def label(self):
                return self.name

            def enum_values(self):
                return dict(self.values)

        match = Property("Match", {"Always": 0, "By Mesh Name": 1})
        antialiasing = Property("Anti Aliasing", {"None": 0, "2x": 2})
        id_source = Property("Color Source", {"Vertex Color": 0, "Material Color": 1})
        output_size = Property("Output Size", {})

        class Parameters:
            changes = []

            @classmethod
            def from_texture_set(cls, _texture_set):
                return cls()

            @classmethod
            def set(cls, changes):
                cls.changes.append(dict(changes))

            def set_textureset_enabled(self, _enabled):
                pass

            def set_enabled_bakers(self, _bakers):
                pass

            def common(self):
                return {
                    "Match": match,
                    "Antialiasing": antialiasing,
                    "OutputSize": output_size,
                }

            def baker(self, _usage):
                return {"ColorSource": id_source}

        self.painter.baking.BakingParameters = Parameters
        self.painter.baking.unlink_all_common_parameters = lambda: None
        request = {
            "strict_bake_settings": True,
            "settings": {
                "resolution": 2048,
                "mesh_maps": ["Normal", "AO"],
                "match": "BY_MESH_NAME",
                "antialiasing": "X2",
                "id_source": "MATERIAL_COLOR",
            },
        }
        self.plugin._configure_baking(request)
        receipt = request["bake_settings_result"]
        self.assertTrue(receipt["exact"])
        self.assertEqual(receipt["configured_texture_set_count"], 1)
        self.assertEqual(
            receipt["texture_sets"]["M_Cushion"],
            {
                "configured": True,
                "set_call_succeeded": True,
                "match": "BY_MESH_NAME",
                "antialiasing": "X2",
                "id_source": "MATERIAL_COLOR",
                "resolution": 2048,
                "observed_labels": {
                    "match": "By Mesh Name",
                    "antialiasing": "2x",
                    "id_source": "Material Color",
                },
            },
        )

    def test_strict_meshy_bake_settings_fail_if_2x_is_unresolvable(self):
        stack = _FakeStack()
        texture_set = _FakeTextureSet("M_Cushion", stack)
        self._install_fake_runtime(texture_set, {stack: []})

        class Property:
            def __init__(self, name, values):
                self.name = name
                self.values = values

            def short_name(self):
                return self.name

            def label(self):
                return self.name

            def enum_values(self):
                return dict(self.values)

        match = Property("Match", {"By Mesh Name": 1})
        id_source = Property("Color Source", {"Material Color": 1})

        class Parameters:
            @classmethod
            def from_texture_set(cls, _texture_set):
                return cls()

            @staticmethod
            def set(_changes):
                pass

            def set_textureset_enabled(self, _enabled):
                pass

            def set_enabled_bakers(self, _bakers):
                pass

            def common(self):
                return {"Match": match}

            def baker(self, _usage):
                return {"ColorSource": id_source}

        self.painter.baking.BakingParameters = Parameters
        self.painter.baking.unlink_all_common_parameters = lambda: None
        with self.assertRaisesRegex(RuntimeError, "antialiasing=2x"):
            self.plugin._configure_baking({
                "strict_bake_settings": True,
                "settings": {
                    "resolution": 2048,
                    "mesh_maps": ["Normal"],
                    "match": "BY_MESH_NAME",
                    "antialiasing": "X2",
                    "id_source": "MATERIAL_COLOR",
                },
            })


if __name__ == "__main__":
    unittest.main()

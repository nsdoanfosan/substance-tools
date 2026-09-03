import importlib.util
import json
import os
import sys
import tempfile
import time
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
            "substance_tools_painter_pending_create_test_target",
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


class _FakeTextureSet:
    def __init__(self, name):
        self.name = name


class PainterPendingCreateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin, cls.painter = _load_plugin_with_import_stubs()

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.local_app_data = Path(self.temporary_directory.name) / "local"
        self.environment_patch = mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.local_app_data)},
            clear=False,
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

        asset_root = Path(self.temporary_directory.name) / "asset"
        self.low_fbx = asset_root / "low" / "Cushion_low.fbx"
        self.texture_dir = asset_root / "texture"
        self.spp = self.texture_dir / "Cushion.spp"
        self.template = asset_root / "templates" / "Unreal Engine.spt"
        self.low_fbx.parent.mkdir(parents=True)
        self.texture_dir.mkdir(parents=True)
        self.template.parent.mkdir(parents=True)
        self.low_fbx.write_bytes(b"fbx")
        self.template.write_bytes(b"spt")

        self.request = {
            "request_id": str(time.time_ns()),
            "pipeline_hash": "pending-create-pipeline",
            "status": "PENDING",
            "action": "CREATE",
            "low_fbx": str(self.low_fbx),
            "spp": str(self.spp),
            "template": str(self.template),
            "texture_dir": str(self.texture_dir),
            "spp_existed": False,
            "strict_bake_settings": True,
            "low_changed": False,
            "changed_high_texture_sets": ["Cushion"],
            "settings_changed": False,
            "rebake_texture_sets": ["Cushion"],
            "settings": {"resolution": 2048},
        }
        self.pending_path = (
            self.local_app_data
            / "SubstanceTools"
            / self.plugin.PENDING_REQUEST_FILENAME
        )
        self._write_pending_request()
        self._write_durable_request_copies()

        self.plugin._processing = False
        self.plugin._active_request = None
        self.plugin._pending_creation_request_id = None
        self.plugin._pending_creation_started_at = 0.0
        self.plugin._startup_resources_ready = True
        self.plugin._pending_replacement_blocked_reason = None
        self.plugin._pending_request_wait_reason = None
        self.plugin._project_ready_idle_scheduled = False
        self.plugin._project_ready_idle_generation = None
        self.plugin._plugin_generation += 1
        self.plugin._started = True
        self.plugin._last_polled_pipeline_hash = None
        self.plugin._last_busy_log_time = 0.0

        project = self.painter.project
        project.is_open = mock.Mock(return_value=True)
        project.is_in_edition_state = mock.Mock(return_value=True)
        project.is_busy = mock.Mock(return_value=False)
        project.file_path = mock.Mock(return_value=None)
        project.last_imported_mesh_path = mock.Mock(
            return_value=str(self.low_fbx)
        )
        project.needs_saving = mock.Mock(return_value=True)
        project.close = mock.Mock()
        project.Metadata = mock.Mock(return_value={})
        project.execute_when_not_busy = mock.Mock(side_effect=lambda callback: callback())
        project.save = mock.Mock(
            side_effect=AssertionError("CREATE must not save before starting its bake")
        )
        project.save_as = mock.Mock(
            side_effect=AssertionError("CREATE must not save_as before starting its bake")
        )

        self.painter.textureset.all_texture_sets = mock.Mock(return_value=[])
        self.plugin.QtCore.QTimer = types.SimpleNamespace(singleShot=mock.Mock())

    def _write_pending_request(self):
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(
            json.dumps(self.request, indent=2),
            encoding="utf-8",
        )

    def _write_durable_request_copies(self):
        for directory in (self.low_fbx.parent, self.texture_dir):
            (directory / self.plugin.REQUEST_FILENAME).write_text(
                json.dumps(self.request, indent=2),
                encoding="utf-8",
            )

    def _assert_no_presave(self):
        self.painter.project.save.assert_not_called()
        self.painter.project.save_as.assert_not_called()

    def test_unsaved_create_claims_matching_pending_ticket_and_starts_bake_once(self):
        with (
            mock.patch.object(self.plugin, "_process_export_request"),
            mock.patch.object(self.plugin, "_start_bake") as start_bake,
        ):
            self.plugin._poll_requests()

        start_bake.assert_called_once()
        accepted = start_bake.call_args.args[0]
        self.assertEqual(accepted["request_id"], self.request["request_id"])
        self.assertTrue(accepted.get("_loaded_from_pending"))
        self.assertTrue(accepted.get("_pending_request_claimed"))
        self.assertTrue(self.pending_path.exists())
        self._assert_no_presave()

    def test_busy_project_resumes_pending_create_once_when_not_busy(self):
        busy = {"value": True}
        callbacks = []
        self.painter.project.is_busy.side_effect = lambda: busy["value"]
        self.painter.project.execute_when_not_busy.side_effect = callbacks.append

        with mock.patch.object(self.plugin, "_start_bake") as start_bake:
            self.plugin._on_project_ready()
            self.assertEqual(len(callbacks), 1)
            self.assertTrue(self.pending_path.exists())
            start_bake.assert_not_called()

            busy["value"] = False
            callbacks[0]()
            self.plugin._on_project_ready()

        self.assertEqual(
            self.painter.project.execute_when_not_busy.call_count,
            1,
        )
        start_bake.assert_called_once()
        self.assertTrue(self.pending_path.exists())
        self._assert_no_presave()

    def test_transient_missing_imported_mesh_keeps_ticket_for_retry(self):
        mesh_available = {"value": False}

        def imported_mesh_path():
            if not mesh_available["value"]:
                raise RuntimeError("mesh path is not available while project settles")
            return str(self.low_fbx)

        self.painter.project.last_imported_mesh_path.side_effect = imported_mesh_path
        with (
            mock.patch.object(self.plugin, "_process_export_request"),
            mock.patch.object(self.plugin, "_start_bake") as start_bake,
        ):
            self.plugin._poll_requests()
            start_bake.assert_not_called()
            self.assertTrue(self.pending_path.exists())

            mesh_available["value"] = True
            self.plugin._poll_requests()

        start_bake.assert_called_once()
        self.assertTrue(self.pending_path.exists())
        self._assert_no_presave()

    def test_different_unsaved_project_cannot_claim_pending_create(self):
        other_mesh = self.low_fbx.with_name("Other_low.fbx")
        self.painter.project.last_imported_mesh_path.return_value = str(other_mesh)

        with (
            mock.patch.object(self.plugin, "_process_export_request"),
            mock.patch.object(self.plugin, "_start_bake") as start_bake,
        ):
            self.plugin._poll_requests()

        start_bake.assert_not_called()
        self.assertTrue(self.pending_path.exists())
        self.assertIsNone(self.plugin._last_polled_pipeline_hash)
        self._assert_no_presave()

    def test_pending_ticket_is_retained_after_request_acceptance(self):
        claim_observations = []
        original_claim = self.plugin._claim_pending_request

        def observe_claim(request):
            claim_observations.append(
                {
                    "accepted": "_accepted_perf" in request,
                    "ticket_exists": self.pending_path.exists(),
                }
            )
            return original_claim(request)

        with (
            mock.patch.object(
                self.plugin,
                "_claim_pending_request",
                side_effect=observe_claim,
            ) as claim_pending,
            mock.patch.object(self.plugin, "_start_bake"),
        ):
            self.plugin._on_project_ready()

        claim_pending.assert_called_once()
        self.assertEqual(
            claim_observations,
            [{"accepted": True, "ticket_exists": True}],
        )
        self.assertTrue(self.pending_path.exists())

    def test_texture_set_prefix_is_normalized_before_bake_configuration_and_call(self):
        texture_set = _FakeTextureSet("M_Cushion")
        self.painter.textureset.all_texture_sets.return_value = [texture_set]
        observed = []

        def configure(_request):
            observed.append(("configure", texture_set.name))

        def evaluate(script):
            observed.append(("bake", texture_set.name, script))

        self.painter.js.evaluate = mock.Mock(side_effect=evaluate)
        self.plugin._processing = True
        self.plugin._active_request = self.request

        with (
            mock.patch.object(
                self.plugin,
                "_configure_baking",
                side_effect=configure,
            ),
            mock.patch.object(self.plugin, "_mark_request_failed"),
        ):
            self.plugin._start_bake(self.request)

        self.assertEqual(texture_set.name, "Cushion")
        self.assertEqual(observed[0], ("configure", "Cushion"))
        self.assertEqual(observed[1][0:2], ("bake", "Cushion"))
        self.assertIn('alg.baking.bake("Cushion")', observed[1][2])
        self._assert_no_presave()

    def test_template_project_path_can_claim_matching_pending_create(self):
        self.painter.project.file_path.return_value = str(self.template)

        with (
            mock.patch.object(self.plugin, "_process_export_request"),
            mock.patch.object(self.plugin, "_start_bake") as start_bake,
        ):
            self.plugin._poll_requests()

        start_bake.assert_called_once()
        accepted = start_bake.call_args.args[0]
        self.assertEqual(accepted["request_id"], self.request["request_id"])
        self.assertTrue(accepted.get("_loaded_from_pending"))
        self.assertTrue(accepted.get("_pending_request_claimed"))
        self.assertTrue(self.pending_path.exists())
        self._assert_no_presave()

    def test_success_save_uses_save_as_when_current_path_is_template(self):
        metadata = mock.Mock()
        self.painter.project.Metadata.return_value = metadata
        self.painter.project.file_path.return_value = str(self.template)
        self.painter.project.save = mock.Mock()
        self.painter.project.save_as = mock.Mock()
        self.painter.ui.UIMode = types.SimpleNamespace(Edition=object())
        self.painter.ui.switch_to_mode = mock.Mock()
        self.plugin._active_request = dict(
            self.request,
            _pending_request_claimed=True,
        )
        self.plugin._processing = True

        with (
            mock.patch.object(self.plugin, "_normalize_texture_set_names"),
            mock.patch.object(self.plugin, "_apply_source_material_layers"),
            mock.patch.object(self.plugin, "_apply_alpha_color_layers"),
            mock.patch.object(self.plugin, "_mark_request_success") as mark_success,
        ):
            self.plugin._save_successful_request()

        self.painter.project.save.assert_not_called()
        self.painter.project.save_as.assert_called_once_with(str(self.spp))
        mark_success.assert_called_once()
        self.assertFalse(self.pending_path.exists())
        self.assertIsNone(self.plugin._active_request)
        self.assertFalse(self.plugin._processing)

    def test_update_cannot_match_unsaved_or_template_project_by_mesh_only(self):
        self.request.update({"action": "UPDATE", "spp_existed": True})
        self._write_pending_request()
        self._write_durable_request_copies()

        for reported_path in (None, str(self.template)):
            with self.subTest(reported_path=reported_path):
                self.painter.project.file_path.return_value = reported_path
                matched, reason = self.plugin._open_project_request_match(self.request)
                self.assertFalse(matched)
                self.assertTrue(
                    "requested .spp" in reason or "project path differs" in reason
                )

        with (
            mock.patch.object(self.plugin, "_process_export_request"),
            mock.patch.object(self.plugin, "_start_bake") as start_bake,
        ):
            self.plugin._poll_requests()

        start_bake.assert_not_called()
        self.assertTrue(self.pending_path.exists())
        self._assert_no_presave()

    def test_replaced_pending_ticket_aborts_acceptance(self):
        loaded = dict(
            self.request,
            _loaded_from_pending=True,
            _loaded_perf=time.perf_counter(),
        )
        replacement = dict(
            self.request,
            request_id=f"{self.request['request_id']}-replacement",
            pipeline_hash="replacement-pipeline",
        )

        def replace_before_claim(_request):
            self.pending_path.write_text(json.dumps(replacement), encoding="utf-8")
            return False

        with (
            mock.patch.object(self.plugin, "_load_request", return_value=loaded),
            mock.patch.object(
                self.plugin,
                "_claim_pending_request",
                side_effect=replace_before_claim,
            ),
            mock.patch.object(self.plugin, "_start_bake") as start_bake,
        ):
            self.plugin._on_project_ready()

        start_bake.assert_not_called()
        persisted = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["request_id"], replacement["request_id"])
        self.assertIsNone(self.plugin._last_polled_pipeline_hash)

    def test_success_cleanup_preserves_a_replacement_pending_ticket(self):
        completed = dict(self.request, _pending_request_claimed=True)
        replacement = dict(
            self.request,
            request_id=f"{self.request['request_id']}-replacement",
            pipeline_hash="replacement-pipeline",
        )
        self.pending_path.write_text(json.dumps(replacement), encoding="utf-8")

        removed = self.plugin._delete_claimed_pending_request(completed)

        self.assertFalse(removed)
        persisted = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["request_id"], replacement["request_id"])

    def test_stale_idle_callback_is_inert_after_plugin_generation_changes(self):
        callbacks = []
        self.painter.project.is_busy.return_value = True
        self.painter.project.execute_when_not_busy.side_effect = callbacks.append

        with mock.patch.object(self.plugin, "_load_request") as load_request:
            self.plugin._on_project_ready()
            self.assertEqual(len(callbacks), 1)

            self.plugin._started = False
            self.plugin._plugin_generation += 1
            self.plugin._project_ready_idle_scheduled = False
            self.plugin._project_ready_idle_generation = None
            callbacks[0]()

        load_request.assert_not_called()

    def test_pending_ticket_survives_when_durable_success_receipt_fails(self):
        self.painter.project.Metadata.return_value = mock.Mock()
        self.painter.project.file_path.return_value = str(self.template)
        self.painter.project.save = mock.Mock()
        self.painter.project.save_as = mock.Mock()
        self.painter.ui.UIMode = types.SimpleNamespace(Edition=object())
        self.painter.ui.switch_to_mode = mock.Mock()
        self.plugin._active_request = dict(
            self.request,
            _pending_request_claimed=True,
        )
        self.plugin._processing = True

        with (
            mock.patch.object(self.plugin, "_normalize_texture_set_names"),
            mock.patch.object(self.plugin, "_apply_source_material_layers"),
            mock.patch.object(self.plugin, "_apply_alpha_color_layers"),
            mock.patch.object(self.plugin, "_mark_request_success", return_value=False),
            mock.patch.object(
                self.plugin,
                "_schedule_successful_save_retry",
                return_value=True,
            ) as schedule_retry,
        ):
            self.plugin._save_successful_request()

        self.painter.project.save_as.assert_called_once_with(str(self.spp))
        schedule_retry.assert_called_once()
        self.assertTrue(self.pending_path.exists())
        self.assertIsNotNone(self.plugin._active_request)
        self.assertTrue(self.plugin._processing)

    def test_stale_request_callback_is_inert_after_reload_or_replacement(self):
        request = dict(self.request)
        self.plugin._active_request = request
        callback_target = mock.Mock()

        stale_generation_callback = self.plugin._guard_async(
            callback_target,
            request,
        )
        self.plugin._plugin_generation += 1
        stale_generation_callback("reload-result")

        self.plugin._active_request = request
        replaced_request_callback = self.plugin._guard_async(
            callback_target,
            request,
        )
        self.plugin._active_request = dict(
            request,
            request_id=f"{request['request_id']}-new",
        )
        replaced_request_callback("save-result")

        callback_target.assert_not_called()

    def test_guarded_save_retry_is_inert_after_plugin_close(self):
        callbacks = []
        request = dict(self.request)
        self.plugin._active_request = request
        self.plugin.QtCore.QTimer.singleShot.side_effect = (
            lambda _delay, callback: callbacks.append(callback)
        )

        with mock.patch.object(self.plugin, "_save_successful_request") as save:
            scheduled = self.plugin._schedule_successful_save_retry(
                request,
                "Painter is busy",
            )
            self.assertTrue(scheduled)
            self.assertEqual(len(callbacks), 1)

            self.plugin._started = False
            self.plugin._plugin_generation += 1
            self.plugin._active_request = None
            callbacks[0]()

        save.assert_not_called()

    def test_update_save_rechecks_the_open_project_before_mutation_and_save(self):
        update = dict(
            self.request,
            action="UPDATE",
            spp_existed=True,
        )
        other_spp = self.spp.with_name("Other.spp")
        self.plugin._active_request = update
        self.plugin._processing = True
        self.plugin._last_polled_pipeline_hash = update["request_id"]
        self.painter.project.file_path.return_value = str(other_spp)
        self.painter.project.save = mock.Mock()

        with (
            mock.patch.object(self.plugin, "_normalize_texture_set_names") as normalize,
            mock.patch.object(self.plugin, "_apply_source_material_layers") as apply_source,
            mock.patch.object(self.plugin, "_apply_alpha_color_layers") as apply_alpha,
            mock.patch.object(self.plugin, "_mark_request_success") as mark_success,
        ):
            self.plugin._save_reimported_request()

        normalize.assert_not_called()
        apply_source.assert_not_called()
        apply_alpha.assert_not_called()
        self.painter.project.save.assert_not_called()
        mark_success.assert_not_called()
        self.assertIsNone(self.plugin._last_polled_pipeline_hash)
        self.assertIsNone(self.plugin._active_request)
        self.assertFalse(self.plugin._processing)

    def test_pending_restore_never_overwrites_a_newer_interleaved_ticket(self):
        completed = dict(self.request, _pending_request_claimed=True)
        displaced = dict(
            self.request,
            request_id=f"{self.request['request_id']}-displaced",
            pipeline_hash="displaced-pipeline",
        )
        newest = dict(
            self.request,
            request_id=f"{self.request['request_id']}-newest",
            pipeline_hash="newest-pipeline",
        )
        self.pending_path.write_text(json.dumps(displaced), encoding="utf-8")

        def publish_newer_then_fail_link(_source, destination):
            Path(destination).write_text(json.dumps(newest), encoding="utf-8")
            raise FileExistsError(destination)

        with mock.patch.object(
            self.plugin.os,
            "link",
            side_effect=publish_newer_then_fail_link,
        ):
            removed = self.plugin._delete_claimed_pending_request(completed)

        self.assertFalse(removed)
        persisted = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["request_id"], newest["request_id"])
        moved = list(self.pending_path.parent.glob(".*.completed"))
        self.assertEqual(len(moved), 1)
        preserved = json.loads(moved[0].read_text(encoding="utf-8"))
        self.assertEqual(preserved["request_id"], displaced["request_id"])


if __name__ == "__main__":
    unittest.main()

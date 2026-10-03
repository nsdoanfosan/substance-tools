"""Admission and native receipts, without starting Blender or Painter."""
import ast
import copy
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from test_painter_pending_create import _load_plugin_with_import_stubs


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'painter/startup/substance_tools_unreal_viewport/workstation_coordination.py'


def load_helper():
    spec = importlib.util.spec_from_file_location('substance_workstation_test_helper', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata():
    return dict(phase_id='phase-one', ticket_id='ticket-one', provider='Codex',
                session_id='owner', resource='painter:main', pipeline='substance-tools',
                request_id='native-one', target='c:/work/asset.spp')


def coordinated_request():
    return dict(request_id='native-one', pipeline_hash='native-hash', status='PENDING',
                spp='C:/work/asset.spp', workstation_phase=metadata())


def publish_worker(path, request_id, start, connection):
    helper = load_helper()
    connection.send('ready')
    start.wait(10)
    try:
        helper.publish_pending(path, dict(request_id=request_id, status='PENDING', spp='asset.spp'))
        connection.send(('published', request_id))
    except RuntimeError:
        connection.send(('waiting', request_id))
    finally:
        connection.close()


def lock_worker(path, connection):
    helper = load_helper()
    with helper._publication_lock(path):
        connection.send('locked')
        connection.recv()  # Parent terminates the process to test OS release.


class NativePublicationTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper()
        temporary = tempfile.TemporaryDirectory(prefix='substance-native-publication-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'pending.json'

    def test_exact_repeated_dispatch_preserves_saved_success(self):
        original = dict(request_id='same', status='SUCCESS', receipt='native saved')
        self.path.write_text(json.dumps(original), encoding='utf-8')
        before = self.path.read_bytes()
        self.assertFalse(self.helper.publish_pending(self.path, dict(request_id='same', status='PENDING')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_terminal_old_request_allows_normal_slot_reuse(self):
        self.path.write_text(json.dumps(dict(request_id='old', status='SUCCESS')), encoding='utf-8')
        request = dict(request_id='next', status='PENDING', spp='new.spp')
        self.assertTrue(self.helper.publish_pending(self.path, request))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), request)
        self.assertFalse(list(self.path.parent.glob('*.publishing')))

    def test_two_processes_publish_exactly_one_request_without_overwrite(self):
        context = multiprocessing.get_context('spawn')
        start = context.Event()
        pipes = [context.Pipe() for _ in range(2)]
        processes = [context.Process(target=publish_worker, args=(str(self.path), marker, start, pipe[1]))
                     for marker, pipe in zip(('first', 'second'), pipes)]
        try:
            for process in processes:
                process.start()
            for parent, child in pipes:
                self.assertTrue(parent.poll(10), 'publisher did not start')
                self.assertEqual(parent.recv(), 'ready')
            start.set()
            results = []
            for parent, child in pipes:
                self.assertTrue(parent.poll(10), 'publisher did not finish')
                results.append(parent.recv())
            self.assertEqual(sorted(status for status, marker in results), ['published', 'waiting'])
            winner = next(marker for status, marker in results if status == 'published')
            self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['request_id'], winner)
        finally:
            for process in processes:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
                self.assertEqual(process.exitcode, 0)
                process.close()
            for parent, child in pipes:
                parent.close()
                child.close()

    def test_process_crash_releases_publication_lock(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=lock_worker, args=(str(self.path), child))
        try:
            process.start()
            self.assertTrue(parent.poll(10), 'lock owner did not start')
            self.assertEqual(parent.recv(), 'locked')
            with self.assertRaisesRegex(RuntimeError, 'being published'):
                self.helper.publish_pending(self.path, dict(request_id='blocked', status='PENDING'))
            process.terminate()
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertTrue(self.helper.publish_pending(self.path, dict(request_id='after-crash', status='PENDING')))
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
            process.close()
            parent.close()
            child.close()

    def test_lock_initialization_contention_is_a_wait_not_a_buffered_write_crash(self):
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.seek.return_value = 0
        handle.write.side_effect = PermissionError(13, 'another publisher owns the first byte')
        with mock.patch.object(Path, 'open', return_value=handle) as open_file:
            with self.assertRaisesRegex(RuntimeError, 'being published'):
                with self.helper._publication_lock(self.path):
                    self.fail('lock must not be admitted')
        open_file.assert_called_once_with('a+b', buffering=0)
        handle.__exit__.assert_called_once()

    def test_cleanup_never_deletes_a_newer_native_request(self):
        newer = dict(request_id='newer', status='PENDING')
        self.path.write_text(json.dumps(newer), encoding='utf-8')
        self.assertFalse(self.helper.discard_pending(self.path, dict(request_id='old')))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), newer)
        self.assertTrue(self.helper.discard_pending(self.path, newer))
        self.assertFalse(self.path.exists())

    def test_copy_preflight_preserves_every_destination_before_publication(self):
        other = self.path.parent / 'second.json'
        original = json.dumps(dict(request_id='already-running', status='PENDING')).encode()
        other.write_bytes(original)
        with self.assertRaises(RuntimeError):
            self.helper.publish_request_copies([self.path, other], dict(request_id='new', status='PENDING'))
        self.assertFalse(self.path.exists())
        self.assertEqual(other.read_bytes(), original)

    def test_request_copies_share_exact_marker_and_retain_completed_receipt(self):
        other = self.path.parent / 'second.json'
        request = dict(request_id='native-copy', status='PENDING')
        self.assertEqual(self.helper.publish_request_copies([self.path, other], request), [True, True])
        other.write_text(json.dumps(dict(request, status='SUCCESS')), encoding='utf-8')
        before = other.read_bytes()
        self.assertEqual(self.helper.publish_request_copies([self.path, other], request), [False, False])
        self.assertEqual(other.read_bytes(), before)

    def test_late_receipt_cannot_overwrite_new_request_or_recovered_ticket(self):
        old = coordinated_request()
        newer = dict(old, request_id='new-native')
        for current in (newer, dict(old, workstation_phase=dict(metadata(), ticket_id='new-execution'))):
            with self.subTest(current=current):
                original = json.dumps(current).encode()
                self.path.write_bytes(original)
                self.assertFalse(self.helper.write_receipt(self.path, old, dict(old, status='SUCCESS')))
                self.assertEqual(self.path.read_bytes(), original)

    def test_exact_native_receipt_updates_its_matching_file(self):
        request = coordinated_request()
        self.path.write_text(json.dumps(request), encoding='utf-8')
        receipt = dict(request, status='SUCCESS', native_save='verified')
        self.assertTrue(self.helper.write_receipt(self.path, request, receipt))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), receipt)


class CoordinationHelperTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper()
        self.bridge = mock.Mock()
        self.bridge.bind_handoff.return_value = metadata()
        self.bridge.can_execute_handoff.return_value = True
        self.bridge.heartbeat_handoff.return_value = True
        self.bridge.complete_handoff.return_value = True
        self.bridge.fail_handoff.return_value = True
        patch = mock.patch.object(self.helper, '_bridge', return_value=self.bridge)
        patch.start()
        self.addCleanup(patch.stop)

    def test_legacy_requests_do_not_load_queue_or_change_payload(self):
        request = dict(request_id='manual', spp='manual.spp')
        self.assertIs(self.helper.attach_phase(request, ''), request)
        self.assertIsNone(self.helper.require_phase(''))
        self.assertTrue(self.helper.can_execute(request))
        self.assertTrue(self.helper.heartbeat(request))
        self.assertTrue(self.helper.complete(request))
        self.assertTrue(self.helper.fail(request, 'manual'))
        self.bridge.assert_not_called()

    def test_binding_keeps_execution_ticket_and_exact_native_marker(self):
        request = dict(request_id='native-one', spp='C:/work/asset.spp')
        self.helper.attach_phase(request, 'phase-one')
        self.assertEqual(request['workstation_phase'], metadata())
        self.assertEqual(request['workstation_phase']['ticket_id'], 'ticket-one')
        self.bridge.require_active_phase.assert_called_once_with('phase-one', 'painter')
        self.bridge.require_scopes.assert_called_once_with('phase-one', ['editor'])
        self.bridge.bind_handoff.assert_called_once_with('phase-one', 'substance-tools', 'native-one', 'C:/work/asset.spp')
        self.assertTrue(self.helper.can_execute(request))

    def test_incomplete_or_secret_binding_is_rejected_before_publication(self):
        for invalid in ({key: value for key, value in metadata().items() if key != 'ticket_id'},
                        dict(metadata(), token='secret')):
            with self.subTest(invalid=invalid):
                self.bridge.bind_handoff.return_value = invalid
                request = dict(request_id='native-one', spp='C:/work/asset.spp')
                with self.assertRaises(RuntimeError):
                    self.helper.attach_phase(request, 'phase-one')
                self.assertNotIn('workstation_phase', request)

    def test_changed_target_or_native_id_cannot_execute(self):
        for key, value in [('spp', 'other.spp'), ('request_id', 'new-native')]:
            request = coordinated_request()
            request[key] = value
            original = copy.deepcopy(request)
            self.assertFalse(self.helper.can_execute(request))
            self.assertEqual(request, original)
        self.bridge.can_execute_handoff.assert_not_called()

    def test_bridge_failure_preserves_native_request(self):
        request = coordinated_request()
        original = copy.deepcopy(request)
        self.bridge.can_execute_handoff.side_effect = RuntimeError('queue unavailable')
        self.assertFalse(self.helper.can_execute(request))
        self.assertEqual(request, original)

    def test_heartbeat_throttles_attempts_even_when_bridge_is_unavailable(self):
        request = coordinated_request()
        self.bridge.heartbeat_handoff.return_value = False
        with mock.patch.object(self.helper.time, 'monotonic', side_effect=[0, 44.9, 45]):
            self.assertFalse(self.helper.heartbeat(request))
            self.assertTrue(self.helper.heartbeat(request))
            self.assertFalse(self.helper.heartbeat(request))
        self.assertEqual(self.bridge.heartbeat_handoff.call_count, 2)

    def test_completion_and_failure_report_actual_bridge_outcome(self):
        request = coordinated_request()
        self.assertTrue(self.helper.complete(request))
        self.bridge.complete_handoff.assert_called_once_with(metadata(), 'native-one', 'c:/work/asset.spp')
        self.bridge.fail_handoff.return_value = False
        self.assertFalse(self.helper.fail(request, 'late callback'))
        self.bridge.fail_handoff.assert_called_once_with(metadata(), 'late callback')

    def test_apply_binding_uses_original_blender_target(self):
        self.bridge.bind_handoff.return_value = dict(metadata(), phase_id='apply', resource='blender',
                                                    pipeline=self.helper.APPLY_PIPELINE, target='c:/work/asset.blend')
        request = dict(request_id='native-one', spp='C:/work/asset.spp')
        self.helper.attach_phase(request, 'apply', pipeline=self.helper.APPLY_PIPELINE,
                                 target='C:/work/asset.blend', resource='blender')
        self.assertEqual(request['workstation_target'], 'C:/work/asset.blend')
        self.assertTrue(self.helper.can_execute(request))
        self.helper.complete(request)
        self.bridge.complete_handoff.assert_called_once_with(request['workstation_phase'], 'native-one', 'c:/work/asset.blend')

    def test_apply_binding_rejects_other_blender_resource_before_binding(self):
        request = dict(request_id='native-one', spp='C:/work/asset.spp')
        with self.assertRaisesRegex(ValueError, 'different original Blender file'):
            self.helper.attach_phase(request, 'apply', pipeline=self.helper.APPLY_PIPELINE,
                                     target='C:/work/asset.blend', resource='blender:C:/other.blend')
        self.bridge.bind_handoff.assert_not_called()
        self.bridge.require_active_phase.assert_not_called()
        self.assertNotIn('workstation_phase', request)

    def test_followup_resource_target_validation_precedes_admission(self):
        request = coordinated_request()
        self.bridge.Store.return_value.phases_snapshot.return_value = [dict(id='apply', resource='blender:C:/other.blend')]
        with self.assertRaisesRegex(ValueError, 'different original Blender file'):
            self.helper.start_followup(request, 'apply', target='C:/work/asset.blend')
        self.bridge.start_followup_phase.assert_not_called()
        for resource in ('blender', 'blender:c:\\work\\asset.blend'):
            self.bridge.Store.return_value.phases_snapshot.return_value = [dict(id='apply', resource=resource)]
            self.helper.start_followup(request, 'apply', target='C:/work/asset.blend')
        self.assertEqual(self.bridge.start_followup_phase.call_count, 2)

    def test_pending_collision_preserves_exact_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pending.json'
            original = json.dumps(coordinated_request()).encode()
            path.write_bytes(original)
            with self.assertRaises(RuntimeError):
                self.helper.preflight_pending(path, phase_id='different', target='other.spp')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.helper.preflight_pending(path, phase_id='phase-one', target='c:\\work\\asset.spp'),
                             coordinated_request())
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_pending_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pending.json'
            for original in (b'{broken', b'[]'):
                path.write_bytes(original)
                with self.assertRaises(RuntimeError):
                    self.helper.preflight_pending(path)
                self.assertEqual(path.read_bytes(), original)


class BridgeLoaderTests(unittest.TestCase):
    def test_source_reload_avoids_bytecode_cache_and_preserves_old_globals(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, WORKSTATION_QUEUE_REPO=directory):
            source = Path(directory) / 'pipeline_bridge.py'
            source.write_text('RULE = "old"\ndef rule(): return RULE\n', encoding='utf-8')
            with mock.patch.dict(sys.modules, {name: None for name in ('queue_store', 'work_phases', 'wq_paths', 'machine_capacity')}):
                old = helper._bridge()
                stamp = source.stat().st_mtime_ns
                source.write_text('RULE = "new"\ndef rule(): return RULE\n', encoding='utf-8')
                os.utime(source, ns=(stamp + 100, stamp + 100))
                new = helper._bridge()
            self.assertEqual(old.rule(), 'old')
            self.assertEqual(new.rule(), 'new')
            self.assertIsNot(old, new)
            self.assertNotIn(directory, sys.path)

    def test_cached_queue_from_another_repository_is_rejected(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, WORKSTATION_QUEUE_REPO=directory):
            (Path(directory) / 'pipeline_bridge.py').write_text('RULE = 1\n', encoding='utf-8')
            collision = types.SimpleNamespace(__file__=str(Path(directory) / 'wrong' / 'queue_store.py'))
            with mock.patch.dict(sys.modules, queue_store=collision):
                with self.assertRaisesRegex(RuntimeError, 'module collision: queue_store'):
                    helper._bridge()


class PainterNativeGateTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.painter = _load_plugin_with_import_stubs()
        self.helper = mock.Mock()
        self.helper.can_execute.return_value = False
        self.helper.write_receipt.side_effect = load_helper().write_receipt
        patch = mock.patch.object(self.plugin, '_workstation_helper', return_value=self.helper)
        patch.start()
        self.addCleanup(patch.stop)
        self.request = coordinated_request()
        self.plugin._last_polled_pipeline_hash = 'old'
        self.plugin._processing = False
        self.plugin._active_request = None
        self.plugin._started = True
        self.plugin._startup_resources_ready = True

    def test_pending_project_wait_does_not_open_close_claim_or_rewrite(self):
        original = copy.deepcopy(self.request)
        self.painter.project.is_open = mock.Mock(side_effect=AssertionError('native project access before admission'))
        with mock.patch.object(self.plugin, '_load_pending_request', return_value=self.request), \
             mock.patch.object(self.plugin, '_delete_claimed_pending_request') as delete:
            self.plugin._create_pending_project()
        self.helper.can_execute.assert_called_once_with(self.request)
        self.painter.project.is_open.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(self.request, original)
        self.assertEqual(self.plugin._last_polled_pipeline_hash, 'old')

    def test_project_ready_wait_does_not_take_marker_or_processing_ownership(self):
        self.painter.project.is_busy = lambda: False
        self.painter.project.is_in_edition_state = lambda: True
        with mock.patch.object(self.plugin, '_load_request', return_value=self.request):
            self.plugin._on_project_ready(None)
        self.assertFalse(self.plugin._processing)
        self.assertIsNone(self.plugin._active_request)
        self.assertEqual(self.plugin._last_polled_pipeline_hash, 'old')

    def test_plugin_reload_does_not_replay_terminal_native_receipts(self):
        self.painter.project.is_busy = lambda: False
        for status in ('SUCCESS', 'FAILED'):
            with self.subTest(status=status):
                request = dict(self.request, status=status)
                self.plugin._last_polled_pipeline_hash = None
                with mock.patch.object(self.plugin, '_load_request', return_value=request):
                    self.plugin._on_project_ready(None)
                self.assertFalse(self.plugin._processing)
                self.assertIsNone(self.plugin._active_request)
                self.assertIsNone(self.plugin._last_polled_pipeline_hash)
        self.helper.can_execute.assert_not_called()

    def test_bake_wait_schedules_native_retry_before_baking_configuration(self):
        with mock.patch.object(self.plugin, '_single_shot_guarded') as retry, \
             mock.patch.object(self.plugin, '_configure_baking') as configure:
            self.plugin._start_bake(self.request)
        configure.assert_not_called()
        retry.assert_called_once_with(500, self.plugin._start_bake, self.request)
        self.assertEqual(self.request, coordinated_request())

    def test_export_wait_keeps_unclaimed_native_request(self):
        with tempfile.TemporaryDirectory() as directory:
            spp = Path(directory) / 'asset.spp'
            path = spp.parent / self.plugin.EXPORT_REQUEST_FILENAME
            original = json.dumps(self.request).encode()
            path.write_bytes(original)
            self.plugin._export_processing = False
            self.plugin._last_export_request_id = 'old'
            self.painter.project.is_open = lambda: True
            self.painter.project.file_path = lambda: str(spp)
            with mock.patch.object(self.plugin.os, 'replace') as claim:
                self.plugin._process_export_request()
            claim.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.plugin._last_export_request_id, 'old')

    def test_only_saved_success_callback_releases_after_exact_receipt_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.json'
            path.write_text(json.dumps(self.request), encoding='utf-8')
            def complete(request):
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'SUCCESS')
                self.assertEqual(request['request_id'], 'native-one')
                return True
            self.helper.complete.side_effect = complete
            with mock.patch.object(self.plugin, '_matching_request_paths', return_value=[path]):
                self.assertTrue(self.plugin._mark_request_success(self.request, verified_noop=True))
                self.helper.complete.assert_not_called()
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['native_completion_kind'], 'verified_noop')
                self.assertTrue(saved['workstation_completion_requires_owner'])
                self.assertTrue(self.plugin._mark_request_success(self.request, complete_phase=True))
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['native_completion_kind'], 'saved')
                self.assertNotIn('workstation_completion_requires_owner', saved)
            self.helper.complete.assert_called_once_with(self.request)

    def test_replaced_request_during_saved_callback_is_preserved_and_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.json'
            replacement = dict(self.request, request_id='new-native', status='PENDING')
            original = json.dumps(replacement).encode()
            path.write_bytes(original)
            with mock.patch.object(self.plugin, '_matching_request_paths', return_value=[path]):
                self.assertFalse(self.plugin._mark_request_success(self.request, complete_phase=True))
            self.assertEqual(path.read_bytes(), original)
            self.helper.complete.assert_not_called()

    def _assert_failed_save_receipt_recovers(self, callback, completion_text):
        self.helper.can_execute.return_value = True
        reservation = {'state': 'active'}
        def fail(request, note):
            reservation['state'] = 'recovery_required'
            return True
        self.helper.fail.side_effect = fail
        self.plugin._active_request = self.request
        self.plugin._processing = True
        self.painter.project.save = mock.Mock()
        self.painter.project.Metadata = mock.Mock(return_value=mock.Mock())
        self.painter.ui.switch_to_mode = mock.Mock()
        with mock.patch.object(self.plugin, '_open_project_request_match', return_value=(True, 'same SPP')), \
             mock.patch.object(self.plugin, '_normalize_texture_set_names'), \
             mock.patch.object(self.plugin, '_apply_source_material_layers'), \
             mock.patch.object(self.plugin, '_apply_alpha_color_layers'), \
             mock.patch.object(self.plugin, '_mark_request_success', return_value=False) as receipt, \
             mock.patch.object(self.plugin, '_matching_request_paths', return_value=[]), \
             mock.patch.object(self.plugin, '_log') as log:
            getattr(self.plugin, callback)()
        receipt.assert_called_once_with(self.request, complete_phase=True)
        self.painter.project.save.assert_called_once()
        self.painter.ui.switch_to_mode.assert_not_called()
        self.helper.complete.assert_not_called()
        self.assertEqual(reservation['state'], 'recovery_required')
        self.helper.fail.assert_called_once()
        self.assertFalse(any(completion_text in call.args[0] for call in log.call_args_list))

    def test_normalization_saved_without_exact_receipt_enters_recovery(self):
        self._assert_failed_save_receipt_recovers('_save_normalized_request', 'Texture Set names normalized and project saved')

    def test_maps_saved_without_exact_receipt_enters_recovery(self):
        self._assert_failed_save_receipt_recovers('_save_applied_maps_request', 'Source material / Alpha maps applied and project saved')

    def test_reimport_saved_without_exact_receipt_enters_recovery(self):
        self._assert_failed_save_receipt_recovers('_save_reimported_request', 'Painter update applied without mesh-map baking; project saved')
        self.assertIsNone(self.plugin._last_polled_pipeline_hash)

    def test_revoked_same_spp_delayed_save_cannot_normalize_apply_layers_or_save(self):
        self.helper.can_execute.return_value = False
        self.plugin._active_request = self.request
        self.plugin._processing = True
        self.painter.project.save = mock.Mock()
        with mock.patch.object(self.plugin, '_open_project_request_match', return_value=(True, 'same SPP')) as match, \
             mock.patch.object(self.plugin, '_normalize_texture_set_names') as normalize, \
             mock.patch.object(self.plugin, '_apply_source_material_layers') as layers, \
             mock.patch.object(self.plugin, '_apply_alpha_color_layers') as alpha, \
             mock.patch.object(self.plugin, '_matching_request_paths', return_value=[]):
            self.plugin._save_applied_maps_request()
        match.assert_not_called()
        normalize.assert_not_called()
        layers.assert_not_called()
        alpha.assert_not_called()
        self.painter.project.save.assert_not_called()
        self.helper.complete.assert_not_called()
        self.helper.fail.assert_called_once()

    def test_revoked_delayed_bake_save_is_not_silently_rescheduled(self):
        self.helper.can_execute.return_value = False
        self.plugin._active_request = self.request
        self.plugin._processing = True
        self.painter.project.is_busy = lambda: False
        self.painter.project.save = mock.Mock()
        with mock.patch.object(self.plugin, '_normalize_texture_set_names') as normalize, \
             mock.patch.object(self.plugin, '_schedule_successful_save_retry') as retry:
            self.plugin._save_successful_request()
        normalize.assert_not_called()
        retry.assert_not_called()
        self.painter.project.save.assert_not_called()
        self.helper.fail.assert_called_once()

    def test_uncertain_coordinated_create_records_failure_and_does_not_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / 'pending.json'
            template = Path(directory) / 'Unreal.spt'
            template.write_bytes(b'template')
            request = dict(self.request, action='CREATE', template=str(template), low_fbx='low.fbx',
                           texture_dir=directory, settings={'resolution': 2048})
            pending.write_text(json.dumps(request), encoding='utf-8')
            reservation = {'state': 'active'}
            self.helper.can_execute.side_effect = lambda payload: reservation['state'] == 'active'
            self.helper.fail.side_effect = lambda payload, note: reservation.update(state='recovery_required') or True
            self.painter.project.is_open = lambda: False
            self.painter.project.Settings = mock.Mock()
            self.painter.project.AutoUnwrapSettings = mock.Mock()
            self.painter.project.create = mock.Mock(side_effect=RuntimeError('native CREATE outcome uncertain'))
            with mock.patch.object(self.plugin, '_pending_request_path', return_value=pending), \
                 mock.patch.object(self.plugin, '_matching_request_paths', return_value=[]):
                self.plugin._create_pending_project()
                self.plugin._create_pending_project()
            self.painter.project.create.assert_called_once()
            recorded = json.loads(pending.read_text(encoding='utf-8'))
            self.assertEqual(recorded['request_id'], request['request_id'])
            self.assertEqual(recorded['status'], 'FAILED')
            self.assertEqual(recorded['workstation_phase'], metadata())
            self.assertEqual(reservation['state'], 'recovery_required')


def load_operator_class(name, workstation):
    tree = ast.parse((ROOT / 'operators.py').read_text(encoding='utf-8'))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name)
    class Operator:
        def __init__(self):
            self.reports = []
        def report(self, kind, message):
            self.reports.append((kind, message))
    bpy = types.SimpleNamespace(types=types.SimpleNamespace(Operator=Operator),
                                props=types.SimpleNamespace(StringProperty=lambda **kwargs: None,
                                                            EnumProperty=lambda **kwargs: None,
                                                            IntProperty=lambda **kwargs: None),
                                data=types.SimpleNamespace(filepath='C:/work/asset.blend'))
    namespace = dict(bpy=bpy, workstation=workstation, Path=Path, time=time, json=json,
                     __name__='substance_apply_guard_test.operators', __package__='substance_apply_guard_test')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'operators.py', 'exec'), namespace)
    return namespace[name](), namespace


class BlenderNativeGateTests(unittest.TestCase):
    def test_existing_project_api_uses_the_same_atomic_native_publisher(self):
        tree = ast.parse((ROOT / 'existing_project.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'queue_existing_painter_project')
        with tempfile.TemporaryDirectory() as directory:
            spp = Path(directory) / 'existing.spp'
            spp.write_bytes(b'original')
            request = dict(coordinated_request(), action='UPDATE', spp=str(spp))
            core = mock.Mock()
            core.baking_paths.return_value = dict(spp=spp, texture_dir=spp.parent)
            core.PAINTER_REQUEST = 'request.json'
            core.read_json.return_value = request
            helper = mock.Mock()
            namespace = dict(core=core, workstation=helper, Path=Path)
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'existing_project.py', 'exec'), namespace)
            result = namespace['queue_existing_painter_project']()
        helper.preflight_pending.assert_called_once_with(core.pending_request_path(), request_id='native-one')
        published = helper.publish_pending.call_args.args[1]
        self.assertEqual(published['workstation_phase'], metadata())
        self.assertEqual(published['action'], 'UPDATE')
        self.assertTrue(published['open_existing_project'])
        self.assertFalse(published['preserve_open_project'])
        core.write_json.assert_not_called()
        self.assertEqual(result['request_id'], 'native-one')

    def test_initial_admission_rejection_precedes_source_exports(self):
        helper = mock.Mock()
        helper.require_phase.side_effect = RuntimeError('waiting')
        operator, namespace = load_operator_class('ExportBakingToSubstancePainterOperator', helper)
        operator.workstation_phase_id = 'painter-phase'
        self.assertEqual(operator.execute(types.SimpleNamespace()), {'CANCELLED'})
        helper.require_phase.assert_called_once_with('painter-phase')

    def test_coordinated_export_requires_paired_apply_phase_before_dispatch(self):
        helper = mock.Mock()
        operator, namespace = load_operator_class('ExportPainterTexturesAndApplyOperator', helper)
        operator.workstation_phase_id = 'painter-phase'
        operator.workstation_apply_phase_id = ''
        self.assertEqual(operator.execute(types.SimpleNamespace()), {'CANCELLED'})
        helper.require_phase.assert_not_called()

    def test_success_export_waits_for_apply_admission_without_scene_mutation(self):
        helper = mock.Mock()
        helper.start_followup.return_value = dict(started=False, reason='capacity_wait')
        operator, namespace = load_operator_class('ExportPainterTexturesAndApplyOperator', helper)
        operator._workstation_request = coordinated_request()
        operator._workstation_export_result = dict(request_id='native-one', status='SUCCESS', files=['exact.png'])
        operator._request_id = 'native-one'
        operator._deadline = 0
        operator._timer = 'native-timer'
        operator._workstation_blend_file = str(Path(namespace['bpy'].data.filepath).resolve())
        operator._workstation_apply_request = None
        operator._workstation_apply_attempt_at = float('-inf')
        operator.workstation_apply_phase_id = 'apply-phase'
        manager = mock.Mock()
        context = types.SimpleNamespace(window_manager=manager)
        namespace['ensure_baking_collections'] = mock.Mock(side_effect=AssertionError('must not mutate scene while waiting'))
        with mock.patch.object(time, 'monotonic', side_effect=[10, 10, 20]):
            self.assertEqual(operator.modal(context, types.SimpleNamespace(type='TIMER')), {'PASS_THROUGH'})
            self.assertEqual(operator.modal(context, types.SimpleNamespace(type='TIMER')), {'PASS_THROUGH'})
        helper.start_followup.assert_called_once_with(operator._workstation_request, 'apply-phase',
                                                    target=operator._workstation_blend_file)
        helper.attach_phase.assert_not_called()
        namespace['ensure_baking_collections'].assert_not_called()
        manager.event_timer_remove.assert_not_called()
        self.assertEqual(operator._workstation_export_result['files'], ['exact.png'])
        self.assertEqual(operator._timer, 'native-timer')

    def test_admitted_child_revoked_during_validation_cannot_store_state_or_apply_files(self):
        for revoke_at in ('source_validation', 'final_transaction'):
            with self.subTest(revoke_at=revoke_at), tempfile.TemporaryDirectory() as directory:
                helper = mock.Mock()
                helper.APPLY_PIPELINE = 'substance-tools.blender-apply'
                helper.start_followup.return_value = dict(started=True, phase=dict(resource='blender'))
                allowed = {'value': True}
                helper.can_execute.side_effect = lambda payload: allowed['value']
                def attach(request, phase_id, *, pipeline, target, resource):
                    request['workstation_phase'] = dict(metadata(), phase_id=phase_id, ticket_id='apply-ticket',
                                                       pipeline=pipeline, resource=resource, target=target)
                    request['workstation_target'] = target
                    return request
                helper.attach_phase.side_effect = attach
                operator, namespace = load_operator_class('ExportPainterTexturesAndApplyOperator', helper)
                parent = dict(coordinated_request(), texture_dir=directory)
                exact_export = dict(request_id='native-one', status='SUCCESS', files=['exact-staged.png'])
                original_bytes = json.dumps(exact_export).encode()
                result_path = Path(directory) / 'exact-result.json'
                result_path.write_bytes(original_bytes)
                operator._workstation_request = parent
                operator._workstation_export_result = exact_export
                operator._request_id = 'native-one'
                operator._deadline = 0
                operator._timer = 'native-timer'
                operator._workstation_blend_file = str(Path(namespace['bpy'].data.filepath).resolve())
                operator._workstation_apply_request = None
                operator._workstation_apply_attempt_at = float('-inf')
                operator.workstation_apply_phase_id = 'apply-phase'
                manager = mock.Mock()
                scene = types.SimpleNamespace(substance_tools_baking=types.SimpleNamespace(resolution=2048))
                context = types.SimpleNamespace(window_manager=manager, scene=scene)
                namespace['ensure_baking_collections'] = mock.Mock(return_value=(None, 'low', None, None))
                namespace['painter_collection_meshes'] = mock.Mock(return_value=['low-object'])
                namespace['baking_paths'] = lambda: dict(texture_dir=Path(directory))
                namespace['PAINTER_REQUEST'] = 'native-request.json'
                namespace['read_json'] = mock.Mock(return_value=dict(status='SUCCESS'))
                source_contract = dict(source_material_maps={}, source_normal_mesh_maps={},
                                       canonical_texture_sets=[], canonical_output_roles={}, resolution=2048)
                namespace['verified_meshy_painter_source_plans'] = mock.Mock(return_value=source_contract)
                namespace['low_texture_set_names'] = lambda objects: []
                namespace['validate_meshy_painter_source_receipts'] = mock.Mock(return_value={})
                namespace['meshy_expected_source_state'] = mock.Mock(return_value={})
                def revoke_source(result, expected):
                    allowed['value'] = False
                    return {}
                namespace['validate_meshy_export_source_state_receipt'] = revoke_source
                transaction = mock.Mock(side_effect=AssertionError('canonical files must remain untouched'))
                namespace['apply_painter_export_transaction'] = transaction
                namespace['PainterApplyNoMaterialsError'] = type('PainterApplyNoMaterialsError', (Exception,), {})
                package = types.ModuleType('substance_apply_guard_test')
                package.__path__ = []
                pipeline = types.ModuleType('substance_apply_guard_test.meshy_pipeline')
                pipeline.STATE_PROPERTY = 'pipeline-state'
                state = dict(stage='BAKE_BASELINE_ARCHIVED', archive=dict(source_original={}, bake_baseline={}))
                def load_state(scene):
                    if revoke_at == 'final_transaction':
                        allowed['value'] = False
                        return None
                    return copy.deepcopy(state)
                pipeline.load_pipeline_state = load_state
                pipeline.advance_pipeline_state = mock.Mock(side_effect=AssertionError('must not advance revoked state'))
                pipeline.store_pipeline_state = mock.Mock(side_effect=AssertionError('must not write revoked scene'))
                pipeline.verify_source_archive_receipt = mock.Mock()
                contract = types.ModuleType('substance_apply_guard_test.meshy_pipeline_contract')
                contract.verify_immutable_snapshot_set_archive = mock.Mock()
                with mock.patch.dict(sys.modules, {package.__name__: package, pipeline.__name__: pipeline,
                                                   contract.__name__: contract}):
                    outcome = operator.modal(context, types.SimpleNamespace(type='TIMER'))
                self.assertEqual(outcome, {'CANCELLED'})
                pipeline.store_pipeline_state.assert_not_called()
                pipeline.advance_pipeline_state.assert_not_called()
                transaction.assert_not_called()
                helper.complete.assert_not_called()
                helper.fail.assert_called_once()
                self.assertIs(operator._workstation_export_result, exact_export)
                self.assertEqual(result_path.read_bytes(), original_bytes)


    def test_ui_collision_controls_follow_primary_pipeline_controls(self):
        source = (ROOT / 'ui.py').read_text(encoding='utf-8')
        self.assertGreater(source.index('collision_box ='), source.index('st.toggle_base_color_source'))


class RealBridgeContractTests(unittest.TestCase):
    def test_native_painter_receipt_then_authorized_blender_followup(self):
        repo = Path(os.environ.get('WORKSTATION_QUEUE_TEST_REPO',
                                   Path.home() / 'Documents/GitHub/workstation-queue-codex'))
        if not (repo / 'pipeline_bridge.py').is_file():
            self.skipTest('Optional workstation-queue checkout is unavailable')
        # A separate interpreter prevents one repo's cached queue_store from
        # changing any other tests. All native files and the DB are temporary.
        script = '''
import importlib.util, os, tempfile
from pathlib import Path
from unittest import mock
spec = importlib.util.spec_from_file_location('helper', os.environ['SUBSTANCE_COORDINATION_HELPER'])
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
with tempfile.TemporaryDirectory(prefix='substance-queue-contract-') as directory:
    os.environ['WORKSTATION_QUEUE_HOME'] = directory
    bridge = helper._bridge()
    store = bridge.Store(bridge.db_path())
    # Deterministic capacity: this test never queries the user's machine.
    with mock.patch.object(bridge.Store, '_phase_capacity', return_value={'allowed': True, 'reason': 'available'}):
        def phase(key, resource):
            return store.enqueue_phase('Codex', 'native-test-owner', '작업관리/내 작업/native-test.md', key,
                'Native test', 'Preserve exact native work', checkpoint='original checkpoint', resource=resource,
                exclusive=['editor'], reason='stable native target', workload='heavy')['phase']
        parent = phase('painter-export', 'painter')
        admitted = store.begin_phase(parent['id'], 'Codex', 'native-test-owner')
        assert admitted['started'], admitted
        apply = phase('blender-apply', 'blender')
        request = {'request_id': 'exact-native-one', 'spp': str(Path(directory) / 'asset.spp')}
        helper.attach_phase(request, parent['id'])
        assert request['workstation_phase']['ticket_id'] == admitted['ticket']['id']
        assert helper.can_execute(request)
        assert helper.complete(request)
        result = helper.start_followup(request, apply['id'])
        assert result['started'], result
        application = dict(request); application.pop('workstation_phase')
        helper.attach_phase(application, apply['id'], pipeline=helper.APPLY_PIPELINE,
                            target=str(Path(directory) / 'asset.blend'), resource=result['phase']['resource'])
        assert helper.can_execute(application)
        assert helper.complete(application)
        assert not helper.can_execute(request)
        assert not helper.can_execute(application)
        phases = store.phases_snapshot()
        assert all(item['state'] == 'completed' for item in phases), phases
print('exact native lifecycle passed')
'''
        environment = dict(os.environ, WORKSTATION_QUEUE_REPO=str(repo), SUBSTANCE_COORDINATION_HELPER=str(HELPER))
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script], env=environment,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('exact native lifecycle passed', result.stdout)


if __name__ == '__main__':
    unittest.main()

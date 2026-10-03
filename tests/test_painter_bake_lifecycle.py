"""Exercise native plugin dispatch/events/save against fake SDK events and real receipts."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from test_painter_pending_create import _load_plugin_with_import_stubs


class Dispatcher:
    def __init__(self):
        self.listeners = {}

    def connect_strong(self, kind, callback):
        self.listeners.setdefault(kind, []).append(callback)

    def disconnect(self, kind, callback):
        if callback in self.listeners.get(kind, []):
            self.listeners[kind].remove(callback)

    def emit(self, kind, **fields):
        for callback in list(self.listeners.get(kind, [])):
            callback(types.SimpleNamespace(**fields))


class PainterBakeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.sdk = _load_plugin_with_import_stubs()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'request.json'
        self.spp = Path(self.temp.name) / 'Prop.spp'
        self.spp.write_bytes(b'original project')
        self.request = {'request_id': 'synthetic-1', 'action': 'UPDATE',
                        'spp': str(self.spp), 'spp_existed': True,
                        'status': 'PENDING', '_request_path': str(self.path),
                        'workstation_phase': {'phase_id': 'synthetic-phase'},
                        'rebake_texture_sets': ['Wood', 'Metal']}
        self.path.write_text(json.dumps(self.request), encoding='utf-8')
        self.plugin._started = True
        self.plugin._active_request = self.request
        self.plugin._processing = True
        self.addCleanup(self.plugin._close_bake_lifecycle)
        self.helper = self.plugin._workstation_helper()
        for name in ('can_execute', 'heartbeat', 'complete', 'fail'):
            patch = mock.patch.object(self.helper, name, return_value=True)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        self.plugin._strip_texture_set_prefixes = mock.Mock(return_value=[])
        self.plugin._configure_baking = mock.Mock()
        self.plugin._normalize_texture_set_names = mock.Mock()
        self.plugin._apply_source_material_layers = mock.Mock()
        self.plugin._apply_alpha_color_layers = mock.Mock()
        self.plugin._delete_claimed_pending_request = mock.Mock()
        self.sdk.project.is_open = mock.Mock(return_value=True)
        self.sdk.project.file_path = mock.Mock(return_value=str(self.spp))
        self.sdk.project.last_imported_mesh_path = mock.Mock(return_value=None)
        self.sdk.project.is_busy = mock.Mock(return_value=False)
        self.sdk.project.needs_saving = mock.Mock(return_value=False)
        self.sdk.project.Metadata = mock.Mock(return_value=mock.Mock())
        self.sdk.project.execute_when_not_busy = mock.Mock(side_effect=lambda f: f())
        self.sdk.project.save = mock.Mock(side_effect=lambda: self.spp.write_bytes(b'saved project'))
        self.sdk.ui.UIMode = types.SimpleNamespace(Edition='Edition')
        self.sdk.ui.switch_to_mode = mock.Mock()
        self.sdk.baking.BakingStatus = types.SimpleNamespace(Success='Success')
        self.handle = object()
        self.sdk.baking.bake_selected_textures_async = mock.Mock(return_value=self.handle)
        self.dispatcher = Dispatcher()
        self.sdk.event.DISPATCHER = self.dispatcher
        for name in ('BakingProcessAboutToStart', 'BakingProcessProgress', 'BakingProcessEnded'):
            setattr(self.sdk.event, name, object())
        self.timers = []
        self.plugin.QtCore.QTimer = types.SimpleNamespace(
            singleShot=lambda delay, callback: self.timers.append((delay, callback)))
        self.now = 0.0

    def launch(self):
        self.plugin._start_bake(self.request)
        lifecycle = self.plugin._active_bake_lifecycle
        if lifecycle:
            lifecycle.clock = lambda: self.now
            lifecycle.dispatched_at = lifecycle.last_event_at = 0.0
        return lifecycle

    def emit_start(self, handle=None):
        self.dispatcher.emit(self.sdk.event.BakingProcessAboutToStart,
                             stop_source=self.handle if handle is None else handle)

    def emit_progress(self, value):
        self.dispatcher.emit(self.sdk.event.BakingProcessProgress, progress=value)

    def emit_end(self, status='Success'):
        self.dispatcher.emit(self.sdk.event.BakingProcessEnded, status=status)

    def drain_save(self):
        callbacks = [callback for delay, callback in self.timers if delay == 0]
        self.timers = [(delay, callback) for delay, callback in self.timers if delay != 0]
        for callback in callbacks:
            callback()

    def receipt(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def assert_pending(self):
        self.assertEqual(self.receipt()['status'], 'PENDING')
        self.assertTrue(self.plugin._processing)
        self.assertIs(self.plugin._active_request, self.request)
        self.sdk.project.save.assert_not_called()
        self.complete.assert_not_called()

    def test_dispatch_without_start_keeps_pending_without_replay(self):
        lifecycle = self.launch()
        self.assertNotIn('_bake_started_perf', self.request)
        self.assertEqual(self.receipt()['bake_lifecycle']['state'], 'dispatched')
        self.emit_end()  # A global end alone is not permission to save.
        self.now = 31
        lifecycle.check_timeout()
        self.assertEqual(self.receipt()['bake_lifecycle']['diagnostic'], 'native_start_unacknowledged')
        self.assert_pending()
        self.sdk.baking.bake_selected_textures_async.assert_called_once()
        self.fail.assert_not_called()

    def test_matching_start_progress_end_then_deferred_save_and_exact_success(self):
        lifecycle = self.launch()
        self.emit_start(object())
        self.emit_progress(1)
        self.emit_end()
        self.assert_pending()
        self.emit_start()
        self.assertIn('_bake_started_perf', self.request)
        self.emit_progress(.4)
        self.emit_progress(1)
        self.emit_end()
        self.emit_end()
        self.assertEqual(lifecycle.state, 'save_pending')
        self.assert_pending()  # Still inside event stack: no save/no success.
        self.drain_save()
        self.sdk.project.save.assert_called_once()
        self.assertEqual(self.receipt()['status'], 'SUCCESS')
        self.assertEqual(self.receipt()['native_completion_kind'], 'saved')
        self.assertEqual(self.receipt()['bake_lifecycle']['state'], 'saved')
        self.complete.assert_called_once_with(self.request)
        self.assertFalse(self.plugin._processing)
        self.assertIsNone(self.plugin._active_bake_lifecycle)
        self.assertFalse(any(self.dispatcher.listeners.values()))

    def test_events_synchronously_during_sdk_dispatch_are_buffered(self):
        def dispatch():
            self.emit_start()
            self.emit_progress(1)
            self.emit_end()
            return self.handle
        self.sdk.baking.bake_selected_textures_async.side_effect = dispatch
        lifecycle = self.launch()
        self.assertEqual(lifecycle.state, 'save_pending')
        self.assert_pending()
        self.drain_save()
        self.sdk.project.save.assert_called_once()
        self.assertEqual(self.receipt()['status'], 'SUCCESS')

    def test_progress_and_end_timeouts_are_diagnostic_only(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_progress(.5)
        self.now = 601
        self.assertEqual(lifecycle.check_timeout()['diagnostic'], 'native_progress_stalled')
        self.emit_progress(.5)  # Repeated value cannot erase the stall.
        self.assertTrue(lifecycle.snapshot()['recovery_required'])
        self.emit_progress(1)
        self.now += 91
        self.assertEqual(lifecycle.check_timeout()['diagnostic'], 'native_end_unacknowledged')
        self.assert_pending()
        self.sdk.baking.bake_selected_textures_async.assert_called_once()

    def test_other_native_start_makes_global_end_ambiguous(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_start(object())
        self.emit_progress(1)
        self.emit_end()
        self.assertEqual(lifecycle.snapshot()['diagnostic'], 'another_native_bake_started')
        self.assert_pending()

    def test_late_events_reject_generation_request_target_phase_or_owner_change(self):
        for change in ('generation', 'request_id', 'spp', 'phase', 'owner', 'denied', 'project'):
            with self.subTest(change=change):
                # Reset the identity for each independent execution.
                self.plugin._plugin_generation += 1
                self.plugin._active_request = self.request
                self.request['request_id'] = 'synthetic-1'
                self.request['spp'] = str(self.spp)
                self.request['workstation_phase'] = {'phase_id': 'synthetic-phase'}
                self.can_execute.return_value = True
                self.sdk.project.file_path.return_value = str(self.spp)
                self.path.write_text(json.dumps(self.request), encoding='utf-8')
                lifecycle = self.launch()
                self.emit_start()
                before = self.path.read_bytes()
                if change == 'generation': self.plugin._plugin_generation += 1
                elif change == 'request_id': self.request['request_id'] = 'other'
                elif change == 'spp': self.request['spp'] = str(self.spp) + '.other'
                elif change == 'phase': self.request['workstation_phase']['phase_id'] = 'other'
                elif change == 'owner': self.plugin._active_request = dict(self.request)
                elif change == 'denied': self.can_execute.return_value = False
                elif change == 'project': self.sdk.project.file_path.return_value = str(self.spp) + '.other'
                self.emit_progress(1)
                self.emit_end()
                lifecycle.check_timeout()
                self.drain_save()
                self.assertEqual(self.path.read_bytes(), before)
                self.sdk.project.save.assert_not_called()
                self.complete.assert_not_called()
        self.plugin._active_request = self.request

    def test_delayed_save_rejects_changed_phase(self):
        self.launch()
        self.emit_start()
        self.emit_end()
        self.request['workstation_phase']['phase_id'] = 'other'
        self.drain_save()
        self.sdk.project.save.assert_not_called()
        self.complete.assert_not_called()

    def test_save_exception_preserves_ownership_and_does_not_retry(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        self.sdk.project.save.side_effect = RuntimeError('save uncertain')
        self.drain_save()
        self.drain_save()
        self.sdk.project.save.assert_called_once()
        self.assertEqual(self.receipt()['status'], 'PENDING')
        self.assertTrue(lifecycle.snapshot()['recovery_required'])
        self.assertTrue(self.plugin._processing)
        self.complete.assert_not_called()
        self.fail.assert_not_called()

    def test_dirty_project_after_save_cannot_report_success(self):
        self.launch()
        self.emit_start()
        self.emit_end()
        self.sdk.project.needs_saving.return_value = True
        self.drain_save()
        self.assertEqual(self.receipt()['status'], 'PENDING')
        self.assertTrue(self.plugin._processing)
        self.complete.assert_not_called()

    def test_success_receipt_failure_keeps_processing_and_does_not_save_again(self):
        self.launch()
        self.emit_start()
        self.emit_end()
        with mock.patch.object(self.helper, 'write_receipt', return_value=False):
            self.drain_save()
        self.drain_save()
        self.assertEqual(self.receipt()['status'], 'PENDING')
        self.assertTrue(self.plugin._processing)
        self.sdk.project.save.assert_called_once()
        self.complete.assert_not_called()

    def test_owner_reconciliation_requires_external_stop_and_durable_failure(self):
        lifecycle = self.launch()
        self.now = 31
        lifecycle.check_timeout()
        reconcile = self.plugin.acknowledge_native_bake_stopped
        self.assertFalse(reconcile('synthetic-1'))
        self.assertFalse(reconcile('other', external_stop_confirmed=True))
        self.assert_pending()
        with mock.patch.object(self.helper, 'write_receipt', return_value=False):
            self.assertFalse(reconcile('synthetic-1', external_stop_confirmed=True))
        self.assertTrue(self.plugin._processing)
        self.assertTrue(reconcile('synthetic-1', external_stop_confirmed=True))
        self.assertEqual(self.receipt()['status'], 'FAILED')
        self.assertFalse(self.plugin._processing)
        self.assertIsNone(self.plugin._active_request)
        self.assertFalse(any(self.dispatcher.listeners.values()))
        self.sdk.project.save.assert_not_called()
        self.sdk.baking.bake_selected_textures_async.assert_called_once()
        self.complete.assert_not_called()

    def test_save_wait_timeout_and_closed_callbacks(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        self.now = 121
        self.assertEqual(lifecycle.check_timeout()['diagnostic'], 'native_save_unacknowledged')
        self.assert_pending()
        self.plugin._close_bake_lifecycle()
        before = self.path.read_bytes()
        lifecycle.started(types.SimpleNamespace(stop_source=self.handle))
        lifecycle.progress(types.SimpleNamespace(progress=1))
        lifecycle.ended(types.SimpleNamespace(status='Success'))
        self.drain_save()
        self.assertEqual(self.path.read_bytes(), before)
        self.sdk.project.save.assert_not_called()

    def test_sdk_dispatch_exception_cleans_observers_and_records_failure(self):
        self.sdk.baking.bake_selected_textures_async.side_effect = RuntimeError('SDK refused')
        self.launch()
        self.sdk.baking.bake_selected_textures_async.assert_called_once()
        self.assertEqual(self.receipt()['status'], 'FAILED')
        self.assertFalse(self.plugin._processing)
        self.assertIsNone(self.plugin._active_bake_lifecycle)
        self.assertFalse(any(self.dispatcher.listeners.values()))
        self.sdk.project.save.assert_not_called()

    def test_progress_receipts_are_bounded(self):
        self.launch()
        self.emit_start()
        with mock.patch.object(self.helper, 'write_receipt', wraps=self.helper.write_receipt) as writes:
            for tick in range(1001):
                self.emit_progress(tick / 1000)
            self.assertLessEqual(writes.call_count, 11)
        self.assertEqual(self.receipt()['bake_lifecycle']['progress'], 1)

    def test_failed_native_end_does_not_save_or_complete(self):
        self.launch()
        self.emit_start()
        self.emit_end('Cancelled')
        self.assertEqual(self.receipt()['status'], 'FAILED')
        self.assertFalse(self.plugin._processing)
        self.assertFalse(any(self.dispatcher.listeners.values()))
        self.sdk.project.save.assert_not_called()
        self.complete.assert_not_called()

    def test_replaced_durable_phase_is_not_overwritten_or_completed(self):
        self.launch()
        self.emit_start()
        replacement = dict(self.request, workstation_phase={'phase_id': 'new-owner'})
        self.path.write_text(json.dumps(replacement), encoding='utf-8')
        before = self.path.read_bytes()
        self.emit_progress(1)
        self.emit_end()
        self.drain_save()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.plugin._processing)
        self.sdk.project.save.assert_not_called()
        self.complete.assert_not_called()

    def test_plugin_close_invalidates_queued_save_and_late_sdk_events(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        self.sdk.event.ProjectEditionEntered = object()
        self.sdk.event.ShelfCrawlingEnded = object()
        self.plugin.close_plugin()
        before = self.path.read_bytes()
        lifecycle.ended(types.SimpleNamespace(status='Success'))
        self.drain_save()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(self.dispatcher.listeners.values()))
        self.sdk.project.save.assert_not_called()
        self.complete.assert_not_called()

    def test_foreign_start_after_end_blocks_deferred_save(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        self.emit_start(object())
        self.drain_save()
        self.assert_pending()
        self.assertTrue(lifecycle.snapshot()['recovery_required'])

    def test_foreign_start_during_native_save_blocks_completion(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        def save():
            self.emit_start(object())
            self.spp.write_bytes(b'synthetic save completed after foreign start')
        self.sdk.project.save.side_effect = save
        self.drain_save()
        self.sdk.project.save.assert_called_once()
        self.assertEqual(self.spp.read_bytes(), b'synthetic save completed after foreign start')
        self.assertEqual(self.receipt()['status'], 'PENDING')
        self.assertTrue(self.plugin._processing)
        self.assertTrue(lifecycle.snapshot()['recovery_required'])
        self.assertEqual(lifecycle.snapshot()['diagnostic'], 'another_native_bake_started')
        self.complete.assert_not_called()

    def test_foreign_start_during_source_layers_blocks_native_save(self):
        self.launch()
        self.emit_start()
        self.emit_end()
        self.plugin._apply_source_material_layers.side_effect = lambda request: self.emit_start(object())
        self.drain_save()
        self.assert_pending()

    def test_foreign_start_during_success_publication_blocks_completion(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        real_write = self.helper.write_receipt
        def write_with_foreign_start(path, request, receipt):
            if receipt.get('status') == 'SUCCESS':
                self.emit_start(object())
            return real_write(path, request, receipt)
        with mock.patch.object(self.helper, 'write_receipt', side_effect=write_with_foreign_start):
            self.drain_save()
        self.sdk.project.save.assert_called_once()
        self.assertEqual(self.receipt()['status'], 'PENDING')
        self.assertTrue(self.plugin._processing)
        self.assertTrue(lifecycle.snapshot()['recovery_required'])
        self.complete.assert_not_called()

    def test_rebound_failed_receipt_cannot_confirm_old_recovery(self):
        lifecycle = self.launch()
        self.now = 31
        lifecycle.check_timeout()
        real_write = self.helper.write_receipt
        replacement = dict(self.request, workstation_phase={'phase_id': 'new-phase'}, status='FAILED')
        def replace_before_guarded_write(path, request, receipt):
            self.path.write_text(json.dumps(replacement), encoding='utf-8')
            return real_write(path, request, receipt)
        with mock.patch.object(self.helper, 'write_receipt', side_effect=replace_before_guarded_write):
            self.assertFalse(self.plugin.acknowledge_native_bake_stopped(
                'synthetic-1', external_stop_confirmed=True))
        self.assertTrue(self.plugin._processing)
        self.assertIs(self.plugin._active_request, self.request)
        self.assertIs(self.plugin._active_bake_lifecycle, lifecycle)
        self.assertEqual(self.receipt()['workstation_phase'], {'phase_id': 'new-phase'})
        self.fail.assert_not_called()

    def test_terminal_reread_rejects_identity_replacement_after_successful_write(self):
        for field in ('request_id', 'action', 'spp', 'workstation_phase'):
            with self.subTest(field=field):
                self.plugin._close_bake_lifecycle()
                self.plugin._active_request = self.request
                self.path.write_text(json.dumps(self.request), encoding='utf-8')
                lifecycle = self.launch()
                self.now = 31
                lifecycle.check_timeout()
                real_write = self.helper.write_receipt
                def replace_after_write(path, request, receipt):
                    written = real_write(path, request, receipt)
                    replaced = self.receipt()
                    replaced[field] = ({'phase_id': 'replacement-phase'}
                                       if field == 'workstation_phase' else 'replacement')
                    self.path.write_text(json.dumps(replaced), encoding='utf-8')
                    return written
                with mock.patch.object(self.helper, 'write_receipt', side_effect=replace_after_write):
                    self.assertFalse(self.plugin.acknowledge_native_bake_stopped(
                        'synthetic-1', external_stop_confirmed=True))
                self.assertTrue(self.plugin._processing)
                self.assertIs(self.plugin._active_bake_lifecycle, lifecycle)
                self.fail.assert_not_called()

    def test_terminal_reread_cannot_clear_replaced_active_execution(self):
        lifecycle = self.launch()
        self.now = 31
        lifecycle.check_timeout()
        real_write = self.helper.write_receipt
        replacement = dict(self.request)
        def replace_active_after_write(path, request, receipt):
            written = real_write(path, request, receipt)
            self.plugin._active_request = replacement
            return written
        with mock.patch.object(self.helper, 'write_receipt', side_effect=replace_active_after_write):
            self.assertFalse(self.plugin.acknowledge_native_bake_stopped(
                'synthetic-1', external_stop_confirmed=True))
        self.assertTrue(self.plugin._processing)
        self.assertIs(self.plugin._active_request, replacement)
        self.assertIs(self.plugin._active_bake_lifecycle, lifecycle)
        self.fail.assert_not_called()

    def test_busy_retry_exhaustion_holds_without_calling_native_save(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        self.request['_save_retry_count'] = 120
        self.sdk.project.is_busy.return_value = True
        self.drain_save()
        self.assert_pending()
        self.assertTrue(lifecycle.snapshot()['recovery_required'])

    def test_unverifiable_busy_state_holds_without_native_save(self):
        self.launch()
        self.emit_start()
        self.emit_end()
        self.sdk.project.is_busy.side_effect = RuntimeError('busy query unavailable')
        self.drain_save()
        self.assert_pending()
        self.assertEqual(self.receipt()['bake_lifecycle']['diagnostic'],
                         'native_save_busy_state_unverified')

    def test_busy_wait_retries_remain_guarded_then_hold_at_limit(self):
        lifecycle = self.launch()
        self.emit_start()
        self.emit_end()
        self.sdk.project.is_busy.return_value = True
        self.request['_save_retry_count'] = 119
        self.drain_save()
        self.assertEqual(self.request['_save_retry_count'], 120)
        self.assert_pending()
        self.assertFalse(lifecycle.snapshot()['recovery_required'])
        callbacks = [callback for delay, callback in self.timers if delay == 1000]
        for callback in callbacks:
            callback()
        self.assert_pending()
        self.assertEqual(lifecycle.snapshot()['diagnostic'], 'native_save_busy_wait_exhausted')

    def test_single_set_keeps_legacy_javascript_dispatch_and_save(self):
        self.request['rebake_texture_sets'] = ['Wood']
        self.sdk.js.evaluate = mock.Mock()
        self.assertIsNone(self.launch())
        self.sdk.js.evaluate.assert_called_once_with('alg.baking.bake("Wood")')
        self.sdk.baking.bake_selected_textures_async.assert_not_called()
        self.assertEqual([delay for delay, _ in self.timers], [3000])
        self.timers[0][1]()
        self.sdk.project.save.assert_called_once()
        self.assertEqual(self.receipt()['status'], 'SUCCESS')


if __name__ == '__main__':
    unittest.main()

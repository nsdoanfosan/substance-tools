"""Execute the real modal, transaction and journal with fake native state only."""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from test_workstation_coordination import ROOT, load_helper, load_operator_class, metadata


def load_journal():
    spec = importlib.util.spec_from_file_location('apply_receipt_test', ROOT / 'blender_apply_completion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Scene(dict):
    pass


class ApplyCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='substance-apply-save-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.blend = self.root / 'original.blend'
        self.blend.write_bytes(b'unsaved-materials-not-on-disk')
        self.helper = load_helper()
        self.module = load_journal()
        self.scene = Scene(unrelated_user_edit='preserve this')
        self.scene.substance_tools_baking = types.SimpleNamespace(base_color_source='BAKING')
        self.bpy = types.SimpleNamespace(
            data=types.SimpleNamespace(filepath=str(self.blend), is_dirty=True, scenes=[self.scene]),
            ops=mock.Mock())
        binding = dict(metadata(), phase_id='apply', pipeline=self.helper.APPLY_PIPELINE,
                       resource='blender', target=str(self.blend))
        self.queue_state = dict(state='active', ticket='active', binding=binding)
        self.bridge = types.SimpleNamespace(
            can_execute_handoff=lambda item: item == self.queue_state['binding'] and self.queue_state['state'] == 'active',
            heartbeat_handoff=lambda item: True,
            fail_handoff=self.fail_queue,
            complete_handoff=self.complete_queue)
        self.completions = 0
        self.real_store = None
        # The outer test starts this suite in a separate interpreter, with an
        # explicit temp DB and deterministic admission (no machine observation).
        if os.environ.get('SUBSTANCE_TEST_REAL_QUEUE'):
            real = self.helper._bridge()
            self.real_store = real.Store(self.root / 'isolated.sqlite')
            phase = self.real_store.enqueue_phase('Codex', 'test-owner', 'test-note.md', 'apply',
                'Original goal', 'Save the original Blend', checkpoint='original resume checkpoint',
                resource='blender', workload='heavy', exclusive=['editor'], reason='test')['phase']
            with mock.patch.object(real.Store, '_phase_capacity', return_value={'allowed': True}):
                self.real_store.begin_phase(phase['id'], 'Codex', 'test-owner')
            binding = real.bind_handoff(phase['id'], self.helper.APPLY_PIPELINE, 'native-one',
                                        str(self.blend), store=self.real_store)
            self.bridge = types.SimpleNamespace(**{name: self.real_call(real, name) for name in (
                'can_execute_handoff', 'heartbeat_handoff', 'fail_handoff', 'complete_handoff')})
        self.patch = mock.patch.object(self.helper, '_bridge', return_value=self.bridge)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.request = dict(request_id='native-one', spp=str(self.root / 'original.spp'),
                            workstation_target=str(self.blend), workstation_phase=binding,
                            workstation_parent_phase_id='parent')
        self.phase_id = binding['phase_id']
        self.journal = self.module.ApplyJournal(self.helper, self.bpy, self.root / 'journal')
        self.helper.resume_apply = self.journal.resume
        self.helper.begin_apply = self.journal.begin
        self.helper.applied_awaiting_save = self.journal.applied
        self.operator, self.ns = load_operator_class('ExportPainterTexturesAndApplyOperator', self.helper)
        self.ns['bpy'] = self.bpy
        self.operator._workstation_request = dict(request_id='native-one',
            workstation_phase=dict(metadata(), phase_id='parent'), texture_dir=str(self.root))
        self.operator._workstation_export_result = dict(request_id='native-one', status='SUCCESS')
        self.operator._request_id = 'native-one'
        self.operator._workstation_apply_request = self.request
        self.operator._workstation_blend_file = str(self.blend.resolve())
        self.operator._timer = 'timer'
        self.operator.workstation_phase_id = 'parent'
        self.operator.workstation_apply_phase_id = self.phase_id
        self.context = types.SimpleNamespace(scene=self.scene, window_manager=mock.Mock())
        low = types.SimpleNamespace(material_slots=[types.SimpleNamespace(material=types.SimpleNamespace(name='M_Test'))])
        self.ns.update(ensure_baking_collections=lambda scene: (None, 'low', None, None),
            painter_collection_meshes=lambda collection: [low], baking_paths=lambda: {'texture_dir': self.root})
        self.applies = 0
        self.file_commits = 0
        self.material_state = 'original'

        def apply(*args):
            self.applies += 1
            self.material_state = 'new Painter material'
            self.bpy.data.is_dirty = True
            return 1

        def commit(**kwargs):
            self.file_commits += 1
            return ['canonical.png']

        transaction = types.SimpleNamespace(canonical_files=['canonical.png'], commit=commit, rollback=mock.Mock())
        exception = type('PainterApplyNoMaterialsError', (Exception,), {})
        core = ast.parse((ROOT / 'core.py').read_text(encoding='utf8'))
        fn = next(n for n in core.body if isinstance(n, ast.FunctionDef) and n.name == 'apply_painter_export_transaction')
        globals_ = dict(stripped_material_name=lambda name: name, MESHY_PAINTER_CANONICAL_ROLES=['Color'],
            begin_painter_canonical_apply_transaction=lambda *a, **kw: transaction,
            apply_painter_textures_to_low=apply, PainterApplyNoMaterialsError=exception)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'core.py', 'exec'), globals_)
        self.ns.update(apply_painter_export_transaction=globals_[fn.name], PainterApplyNoMaterialsError=exception)
        package = types.ModuleType('substance_apply_guard_test')
        package.__path__ = []
        pipeline = types.ModuleType(package.__name__ + '.meshy_pipeline')
        pipeline.load_pipeline_state = lambda scene: None
        pipeline.STATE_PROPERTY = 'state'
        for name in ('advance_pipeline_state', 'store_pipeline_state', 'verify_source_archive_receipt'):
            setattr(pipeline, name, mock.Mock())
        contract = types.ModuleType(package.__name__ + '.meshy_pipeline_contract')
        contract.verify_immutable_snapshot_set_archive = mock.Mock()
        patch = mock.patch.dict(sys.modules, {m.__name__: m for m in (package, pipeline, contract)})
        patch.start()
        self.addCleanup(patch.stop)

    def real_call(self, bridge, name):
        def call(*args):
            result = getattr(bridge, name)(*args, store=self.real_store)
            if name == 'complete_handoff' and result:
                self.completions += 1
            return result
        return call

    def complete_queue(self, binding, request_id, target):
        if binding != self.queue_state['binding'] or self.queue_state['state'] == 'cancelled':
            return False
        self.completions += 1
        self.queue_state.update(state='completed', ticket='finished')
        return True

    def fail_queue(self, binding, note):
        self.queue_state['state'] = 'recovery_required'
        return True

    def state(self):
        if self.real_store:
            return self.real_store.phases_snapshot()[0]['state']
        return self.queue_state['state']

    def record(self):
        return self.journal._read(self.journal._path(self.phase_id))

    def apply(self):
        return self.operator.modal(self.context, types.SimpleNamespace(type='TIMER'))

    def save(self):
        self.journal.save_pre(str(self.blend))
        # Fake native save: snapshots exactly this scene/material state.
        self.blend.write_text(json.dumps(dict(scene=self.scene, material=self.material_state)))
        self.bpy.data.is_dirty = False
        self.journal.save_post(str(self.blend))

    def test_real_modal_apply_without_save_retains_owner_and_original_disk(self):
        before = self.blend.read_bytes()
        self.assertEqual(self.apply(), {'FINISHED'})
        self.assertEqual(self.record()['state'], 'applied_awaiting_save')
        self.assertEqual(self.state(), 'active')
        self.assertEqual(self.blend.read_bytes(), before)
        self.assertTrue(self.bpy.data.is_dirty)
        self.assertEqual(self.scene['unrelated_user_edit'], 'preserve this')
        self.assertEqual(self.bpy.ops.mock_calls, [])
        self.assertEqual((self.applies, self.file_commits, self.completions), (1, 1, 0))

    def test_exact_native_save_completes_once_and_preserves_goal_checkpoint(self):
        self.apply()
        self.save()
        self.assertEqual(self.state(), 'completed')
        self.assertEqual(self.record()['state'], 'completed')
        self.journal.save_post(str(self.blend))
        self.journal.tick()
        self.assertEqual(self.completions, 1)
        self.assertEqual(json.loads(self.blend.read_text())['material'], 'new Painter material')
        if self.real_store:
            phase = self.real_store.phases_snapshot()[0]
            self.assertEqual(phase['checkpoint'], 'original resume checkpoint')
            self.assertEqual(phase['goal'], 'Save the original Blend')

    def test_save_failure_clears_arm_retains_recovery_and_retries_only_save(self):
        self.apply()
        self.journal.save_pre(str(self.blend))
        self.journal.save_failed(str(self.blend))
        self.blend.write_bytes(b'late output after failure')
        self.journal.save_post(str(self.blend))
        self.assertEqual(self.state(), 'recovery_required')
        self.assertEqual(self.completions, 0)
        self.save()
        self.assertEqual(self.state(), 'completed')
        self.assertEqual(self.applies, 1)

    def test_cancelled_save_dialog_without_native_events_is_still_waiting(self):
        self.apply()
        self.journal.tick()
        self.assertEqual(self.record()['state'], 'applied_awaiting_save')
        self.assertEqual(self.completions, 0)

    def test_save_as_other_target_does_not_release_original(self):
        self.apply()
        other = self.root / 'other.blend'
        other.write_bytes(b'other')
        self.journal.save_pre(str(other))
        self.bpy.data.filepath = str(other)
        other.write_bytes(b'new other file')
        self.journal.save_post(str(other))
        self.assertEqual(self.state(), 'active')
        self.assertEqual(self.completions, 0)

    def test_late_save_callback_after_current_file_switch_is_ignored(self):
        self.apply()
        self.journal.save_pre(str(self.blend))
        self.blend.write_bytes(b'saved to old target')
        self.bpy.data.filepath = str(self.root / 'other.blend')
        self.journal.save_post(str(self.blend))
        self.assertEqual(self.completions, 0)

    def test_changed_request_receipt_cannot_consume_old_save_callback(self):
        self.apply()
        self.journal.save_pre(str(self.blend))
        record = self.record()
        record['nonce'] = 'different-native-apply'
        self.journal._write(self.journal._path(self.phase_id), record)
        self.blend.write_bytes(b'late native save')
        self.journal.save_post(str(self.blend))
        self.assertEqual(self.completions, 0)

    def test_missing_or_undone_scene_marker_does_not_claim_saved_result(self):
        self.apply()
        self.scene.pop(self.module.MARKER)
        self.save()
        self.assertEqual(self.completions, 0)

    def test_callbacks_without_changed_disk_are_not_save_evidence(self):
        self.apply()
        self.journal.save_pre(str(self.blend))
        self.journal.save_post(str(self.blend))
        self.assertEqual(self.record()['state'], 'applied_awaiting_save')
        self.assertEqual(self.completions, 0)

    def test_reload_retry_never_reexports_or_reapplies_unsaved_work(self):
        self.apply()
        self.journal = self.module.ApplyJournal(self.helper, self.bpy, self.root / 'journal')
        self.helper.resume_apply = self.journal.resume
        # Simulate reload of the old on-disk Blend: material/marker lost, but
        # durable journal still blocks replay and requires owner reconciliation.
        self.scene.pop(self.module.MARKER)
        self.material_state = 'original'
        self.assertEqual(self.operator.execute(self.context), {'FINISHED'})
        self.assertEqual((self.applies, self.file_commits, self.completions), (1, 1, 0))
        self.save()
        self.assertEqual(self.completions, 0)

    def test_repeated_modal_cannot_apply_twice(self):
        self.apply()
        self.assertEqual(self.apply(), {'FINISHED'})
        self.assertEqual(self.applies, 1)

    def test_uncertain_apply_after_crash_never_replays(self):
        self.ns['apply_painter_export_transaction'] = mock.Mock(side_effect=RuntimeError('native crash'))
        self.assertEqual(self.apply(), {'CANCELLED'})
        self.assertEqual(self.record()['state'], 'applying')
        self.assertEqual(self.operator.execute(self.context), {'FINISHED'})
        self.ns['apply_painter_export_transaction'].assert_called_once()
        self.assertEqual(self.completions, 0)

    def test_journal_write_failure_prevents_native_mutation(self):
        with mock.patch.object(self.journal, '_write', side_effect=OSError('disk full')):
            self.assertEqual(self.apply(), {'CANCELLED'})
        self.assertEqual(self.applies, 0)
        self.assertEqual(self.completions, 0)

    def test_receipt_write_failure_after_apply_never_replays_or_releases(self):
        write = self.journal._write
        def fail_after_begin(path, record):
            if record['state'] == 'applied_awaiting_save':
                raise OSError('receipt disk full')
            return write(path, record)
        with mock.patch.object(self.journal, '_write', side_effect=fail_after_begin):
            self.assertEqual(self.apply(), {'CANCELLED'})
        self.assertEqual(self.record()['state'], 'applying')
        self.assertEqual(self.operator.execute(self.context), {'FINISHED'})
        self.save()
        self.assertEqual((self.applies, self.completions), (1, 0))

    def test_another_runtime_cannot_repeat_same_phase_application(self):
        self.apply()
        other = self.module.ApplyJournal(self.helper, self.bpy, self.root / 'journal')
        self.assertFalse(other.begin(self.request, self.scene, self.operator._workstation_export_result))
        self.assertEqual(self.applies, 1)

    def test_saved_journal_write_failure_retains_apply_for_explicit_save_retry(self):
        self.apply()
        self.journal.save_pre(str(self.blend))
        self.blend.write_bytes(b'exact save succeeded but receipt disk failed')
        with mock.patch.object(self.journal, '_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.journal.save_post(str(self.blend))
        self.assertEqual(self.record()['state'], 'applied_awaiting_save')
        self.assertEqual(self.completions, 0)
        self.save()
        self.assertEqual((self.applies, self.completions), (1, 1))

    def test_saved_evidence_retries_queue_delivery_after_reload_without_native_work(self):
        self.apply()
        original = self.bridge.complete_handoff
        self.bridge.complete_handoff = lambda *args: False
        self.save()
        self.assertEqual(self.record()['state'], 'saved')
        self.bridge.complete_handoff = original
        self.journal = self.module.ApplyJournal(self.helper, self.bpy, self.root / 'journal')
        self.helper.resume_apply = self.journal.resume
        self.assertEqual(self.operator.execute(self.context), {'FINISHED'})
        self.assertEqual(self.state(), 'completed')
        self.assertEqual((self.applies, self.completions), (1, 1))

    def test_changed_saved_file_invalidates_pending_evidence(self):
        self.apply()
        original = self.bridge.complete_handoff
        self.bridge.complete_handoff = lambda *args: False
        self.save()
        self.bridge.complete_handoff = original
        self.blend.write_bytes(b'replaced by another saved version')
        self.journal.tick()
        self.assertEqual(self.completions, 0)

    def test_late_save_for_ended_execution_cannot_finish_next_ticket(self):
        self.apply()
        if self.real_store:
            with self.real_store._write() as (connection, now):
                connection.execute('UPDATE work_phases SET ticket_id=? WHERE id=?', ('new-ticket', self.phase_id))
        else:
            self.queue_state['binding'] = dict(self.queue_state['binding'], ticket_id='new-ticket')
        self.save()
        self.assertEqual(self.completions, 0)

    def test_generic_completion_api_cannot_bypass_saved_evidence(self):
        self.apply()
        self.assertFalse(self.helper.complete(self.request))
        self.assertEqual(self.completions, 0)

    def test_manual_modal_retains_old_behavior_without_journal_or_save(self):
        self.operator._workstation_request = {}
        self.operator._workstation_apply_request = None
        self.assertEqual(self.apply(), {'FINISHED'})
        self.assertEqual(self.applies, 1)
        self.assertFalse(list((self.root / 'journal').glob('*.json')))
        self.assertEqual(self.completions, 0)
        self.assertEqual(self.bpy.ops.mock_calls, [])

    def test_registered_save_handlers_complete_and_unregister_without_saving(self):
        handlers = types.SimpleNamespace(save_pre=[], save_post=[], save_post_fail=[], load_pre=[],
                                         persistent=lambda function: function)
        timers = mock.Mock()
        timers.is_registered.return_value = False
        self.bpy.app = types.SimpleNamespace(handlers=handlers, timers=timers)
        self.module._journal = self.journal
        with mock.patch.dict(sys.modules, {'bpy': self.bpy}):
            self.module.register(self.helper)
            self.module.register(self.helper)
            self.assertEqual(len(handlers.save_post), 1)
            self.apply()
            handlers.save_pre[0](str(self.blend))
            handlers.load_pre[0]('other.blend')
            self.blend.write_bytes(b'late save after loading')
            handlers.save_post[0](str(self.blend))
            self.assertEqual(self.completions, 0)
            handlers.save_pre[0](str(self.blend))
            self.blend.write_bytes(b'accepted changed persisted blend')
            handlers.save_post[0](str(self.blend))
            self.assertEqual(self.completions, 1)
            self.module.unregister()
            self.assertEqual(handlers.save_pre + handlers.save_post + handlers.save_post_fail + handlers.load_pre, [])
        self.assertEqual(self.bpy.ops.mock_calls, [])


class RealQueueApplyCompletionTests(unittest.TestCase):
    def test_modal_save_lifecycle_against_isolated_real_bridge_database(self):
        repo = Path(os.environ.get('WORKSTATION_QUEUE_TEST_REPO',
                                   Path.home() / 'Documents/GitHub/workstation-queue-codex'))
        if not (repo / 'pipeline_bridge.py').is_file():
            self.skipTest('Optional workstation-queue checkout is unavailable')
        env = dict(os.environ, SUBSTANCE_TEST_REAL_QUEUE='1', WORKSTATION_QUEUE_REPO=str(repo))
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'unittest',
            'test_blender_apply_completion.ApplyCompletionTests', '-v'], cwd=ROOT / 'tests', env=env,
            capture_output=True, text=True, encoding='utf8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()

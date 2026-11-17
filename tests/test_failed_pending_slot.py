"""A native failure must also close the matching shared pending slot (#15).

Runs the real Painter plugin callbacks (``_mark_request_failed`` and
``_mark_request_success``) with the real native publication helpers
(``write_receipt``, ``preflight_pending``, ``publish_pending``) against files
in a temporary folder. Painter, Blender and the queue bridge are stubs.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_painter_pending_create import _load_plugin_with_import_stubs  # noqa: E402
from test_workstation_coordination import load_helper  # noqa: E402


def binding(phase_id='phase-one', request_id='native-one', target='{spp}'):
  return dict(phase_id=phase_id, ticket_id='ticket-' + phase_id, provider='Claude', session_id='owner',
              resource='painter:main', pipeline='substance-tools', request_id=request_id, target=target)


class FailedPendingSlotTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    folder = Path(self.tmp.name)
    self.texture_dir = folder / 'texture'
    self.texture_dir.mkdir()
    self.spp = self.texture_dir / 'asset_SP.spp'
    self.spp.write_bytes(b'spp')
    self.asset_copy = self.texture_dir / '.substance_tools_request.json'
    self.pending = folder / 'SubstanceTools' / 'pending_request.json'
    self.pending.parent.mkdir()

    self.plugin, self.painter = _load_plugin_with_import_stubs()
    self.real = load_helper()
    self.helper = mock.Mock()
    self.helper.write_receipt.side_effect = self.real.write_receipt
    self.helper.fail.return_value = True
    self.helper.complete.return_value = True
    for target, value in (
      ('_workstation_helper', dict(return_value=self.helper)),
      ('_pending_request_path', dict(return_value=self.pending)),
      ('_request_candidates', dict(return_value=[])),
    ):
      patch = mock.patch.object(self.plugin, target, **value)
      patch.start()
      self.addCleanup(patch.stop)

  def request(self, request_id='native-one', phase_id='phase-one', spp=None):
    spp = str(spp or self.spp)
    return dict(request_id=request_id, pipeline_hash='hash-' + request_id, action='UPDATE', status='PENDING',
                open_existing_project=True, spp=spp, texture_dir=str(self.texture_dir),
                rebake_texture_sets=['Ribbon'],
                workstation_phase=binding(phase_id, request_id, spp.lower().replace('\\', '/')))

  def publish_claimed(self, request):
    """State after Painter claimed a pending UPDATE: slot + asset copy, same ticket."""
    self.pending.write_text(json.dumps(request), encoding='utf-8')
    self.asset_copy.write_text(json.dumps(request), encoding='utf-8')
    claimed = dict(request, _request_path=str(self.asset_copy), _loaded_from_pending=True,
                   _pending_request_claimed=True)
    return claimed

  def read(self, path):
    return json.loads(path.read_text(encoding='utf-8'))

  def next_phase_blocked(self):
    try:
      self.real.preflight_pending(self.pending, phase_id='phase-unrelated', target=str(self.spp))
    except RuntimeError:
      return True
    return False

  # -- the defect -------------------------------------------------------------
  def test_claimed_update_failure_closes_the_slot_and_allows_the_next_publication(self):
    claimed = self.publish_claimed(self.request())
    self.plugin._mark_request_failed(claimed, 'Could not start automatic baking: There is nothing to bake')

    for path in (self.asset_copy, self.pending):
      saved = self.read(path)
      self.assertEqual(saved['status'], 'FAILED', path)
      self.assertEqual(saved['request_id'], 'native-one')
      self.assertEqual(saved['failure'], 'Could not start automatic baking: There is nothing to bake')
      self.assertEqual(saved['workstation_phase'], claimed['workstation_phase'])
      self.assertFalse(any(key.startswith('_') for key in saved))
    self.helper.fail.assert_called_once()
    self.assertFalse(self.next_phase_blocked())
    following = self.request('native-two', 'phase-two')
    self.assertTrue(self.real.publish_pending(self.pending, following))
    self.assertEqual(self.read(self.pending)['request_id'], 'native-two')

  def test_failure_without_asset_copy_still_closes_the_matching_slot(self):
    request = self.request()
    self.pending.write_text(json.dumps(request), encoding='utf-8')
    self.plugin._mark_request_failed(dict(request, _pending_request_claimed=True), 'reload failed')
    self.assertEqual(self.read(self.pending)['status'], 'FAILED')
    self.assertFalse(self.asset_copy.exists())

  # -- never overwrite other work ---------------------------------------------
  def assert_slot_untouched(self, before):
    self.assertEqual(self.pending.read_bytes(), before)
    self.assertTrue(self.next_phase_blocked())

  def test_newer_replacement_ticket_is_not_overwritten(self):
    claimed = self.publish_claimed(self.request())
    newer = self.request('native-newer', 'phase-two')
    self.pending.write_text(json.dumps(newer), encoding='utf-8')
    before = self.pending.read_bytes()
    self.plugin._mark_request_failed(claimed, 'old callback')
    self.assert_slot_untouched(before)
    self.assertEqual(self.read(self.asset_copy)['status'], 'FAILED')

  def test_same_request_id_with_another_phase_binding_is_not_overwritten(self):
    claimed = self.publish_claimed(self.request())
    rebound = self.request(phase_id='phase-other')
    self.pending.write_text(json.dumps(rebound), encoding='utf-8')
    before = self.pending.read_bytes()
    self.plugin._mark_request_failed(claimed, 'late callback')
    self.assert_slot_untouched(before)

  def test_same_request_id_for_another_target_is_not_overwritten(self):
    claimed = self.publish_claimed(self.request())
    other = dict(claimed, spp=str(self.texture_dir / 'other_SP.spp'))
    other.pop('_request_path')
    other = {key: value for key, value in other.items() if not key.startswith('_')}
    self.pending.write_text(json.dumps(other), encoding='utf-8')
    before = self.pending.read_bytes()
    self.plugin._mark_request_failed(claimed, 'wrong target')
    self.assert_slot_untouched(before)

  def test_replacement_racing_the_receipt_wins(self):
    claimed = self.publish_claimed(self.request())
    newer = self.request('native-race', 'phase-two')
    real_write = self.real.write_receipt

    def race(path, request, receipt):
      if Path(path) == self.pending:  # Blender publishes between the read and the locked write
        self.pending.write_text(json.dumps(newer), encoding='utf-8')
      return real_write(path, request, receipt)

    self.helper.write_receipt.side_effect = race
    self.plugin._mark_request_failed(claimed, 'raced')
    self.assertEqual(self.read(self.pending), newer)

  def test_write_failure_leaves_the_slot_for_explicit_recovery(self):
    claimed = self.publish_claimed(self.request())
    before = self.pending.read_bytes()
    real_write = self.real.write_receipt

    def fail_on_slot(path, request, receipt):
      if Path(path) == self.pending:
        raise OSError('disk full')
      return real_write(path, request, receipt)

    self.helper.write_receipt.side_effect = fail_on_slot
    with mock.patch.object(self.plugin, '_log') as log:
      self.plugin._mark_request_failed(claimed, 'bake failed')
    self.assert_slot_untouched(before)
    self.assertEqual(self.read(self.asset_copy)['status'], 'FAILED')
    self.helper.fail.assert_called_once()
    self.assertTrue(any('recovery' in str(call) for call in log.call_args_list))

  def test_missing_or_malformed_slot_is_left_alone(self):
    claimed = self.publish_claimed(self.request())
    self.pending.unlink()
    self.plugin._mark_request_failed(claimed, 'no slot')
    self.assertFalse(self.pending.exists())
    self.assertEqual(self.read(self.asset_copy)['status'], 'FAILED')

    claimed = self.publish_claimed(self.request())
    self.pending.write_text('{not json', encoding='utf-8')
    before = self.pending.read_bytes()
    self.plugin._mark_request_failed(claimed, 'malformed slot')
    self.assertEqual(self.pending.read_bytes(), before)

  def test_failure_does_not_touch_painter(self):
    claimed = self.publish_claimed(self.request())
    self.painter.baking = mock.Mock()
    self.painter.project = mock.Mock()
    self.plugin._mark_request_failed(claimed, 'no replay')
    self.assertEqual(self.painter.baking.mock_calls, [])
    self.assertEqual(self.painter.project.mock_calls, [])

  # -- unchanged behaviour ----------------------------------------------------
  def test_success_receipt_keeps_its_existing_slot_handling(self):
    claimed = self.publish_claimed(self.request())
    before = self.pending.read_bytes()
    self.assertTrue(self.plugin._mark_request_success(claimed, complete_phase=True))
    saved = self.read(self.asset_copy)
    self.assertEqual(saved['status'], 'SUCCESS')
    self.assertEqual(saved['native_completion_kind'], 'saved')
    self.assertEqual(self.pending.read_bytes(), before)  # removed later by the claimed-ticket path
    self.helper.complete.assert_called_once()
    self.helper.fail.assert_not_called()


if __name__ == '__main__':
  unittest.main()

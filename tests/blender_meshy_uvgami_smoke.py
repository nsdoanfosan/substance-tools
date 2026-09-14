"""Factory-startup smoke tests for Substance's UVgami API boundary.

These tests replace UVgami's provider-owned service at the resolver boundary.
Substance must only send versioned requests and retain returned receipts; it
must not reach into UVgami's manager, operator, or scene UI properties.
"""

import json
import unittest

try:
  import bpy
except ModuleNotFoundError:
  bpy = None


class FakeUVgamiProvider:
  SERVICE_ID = 'uvgami.unwrap'
  API_VERSION = 1
  PROFILE = 'optcuts_hard_surface_packed_v1'

  def __init__(self):
    self.running = False
    self.receipt_api_version = self.API_VERSION
    self.calls = []
    self.manager_summary = []
    self.ui_state = {
      'engine': 'XATLAS',
      'use_hard_surface': False,
      'margin': 0.125,
    }
    self.applied_profiles = []

  @property
  def api(self):
    return {
      'service_id': self.SERVICE_ID,
      'version': self.API_VERSION,
      'preflight_unwrap': self.preflight_unwrap,
      'begin_unwrap': self.begin_unwrap,
      'poll_unwrap': self.poll_unwrap,
      'inspect_uv_map': self.inspect_uv_map,
      'get_capabilities': self.get_capabilities,
    }

  def _receipt(self, operation, payload, status):
    return {
      **dict(payload or {}),
      'service_id': self.SERVICE_ID,
      'api_version': self.receipt_api_version,
      'operation': operation,
      'status': status,
    }

  @staticmethod
  def _settings(resolution, margin_pixels):
    return {
      'engine': 'OPTCUTS',
      'use_hard_surface': True,
      'transfer_uvs': True,
      'pack_after_unwrap': True,
      'fix_scale': True,
      'combine_uvs': False,
      'resolution': int(resolution),
      'margin_pixels': int(margin_pixels),
      'margin': float(margin_pixels) / float(resolution),
    }

  def _apply_and_restore_provider_ui(self, settings):
    """Model the fact that temporary UI state belongs to the provider."""
    snapshot = dict(self.ui_state)
    try:
      self.ui_state.update(settings)
      self.applied_profiles.append(dict(self.ui_state))
    finally:
      self.ui_state.clear()
      self.ui_state.update(snapshot)

  def get_capabilities(self):
    self.calls.append(('get_capabilities',))
    return self._receipt(
      'get_capabilities',
      {
        'available': True,
        'profiles': [self.PROFILE],
        'asynchronous': True,
        'preserves_input_topology': True,
      },
      'SUCCESS',
    )

  def preflight_unwrap(
    self,
    objects,
    *,
    scene=None,
    resolution=2048,
    margin_pixels=8,
    **_kwargs,
  ):
    settings = self._settings(resolution, margin_pixels)
    self.calls.append(
      ('preflight_unwrap', tuple(obj.name for obj in objects), scene)
    )
    self._apply_and_restore_provider_ui(settings)
    return self._receipt(
      'preflight_unwrap',
      {
        'profile': self.PROFILE,
        'settings': settings,
        'objects': [obj.name for obj in objects],
      },
      'READY',
    )

  def begin_unwrap(
    self,
    objects,
    *,
    scene=None,
    resolution=2048,
    margin_pixels=8,
    **_kwargs,
  ):
    settings = self._settings(resolution, margin_pixels)
    self.calls.append(('begin_unwrap', tuple(obj.name for obj in objects), scene))
    self._apply_and_restore_provider_ui(settings)
    self.running = True
    job = {
      'service_id': self.SERVICE_ID,
      'api_version': self.API_VERSION,
      'job_id': 'fake-uvgami-job',
      'profile': self.PROFILE,
      'objects': [
        {
          'name': obj.name,
          'pointer': int(obj.as_pointer()),
          'polygon_count': len(obj.data.polygons),
        }
        for obj in objects
      ],
      'settings': settings,
    }
    return self._receipt('begin_unwrap', {'job': job}, 'RUNNING')

  @staticmethod
  def _inspect(obj):
    layer = obj.data.uv_layers.active
    valid = bool(
      layer is not None
      and len(layer.data) == len(obj.data.loops)
      and len(obj.data.polygons) > 0
    )
    return {
      'valid': valid,
      'layer': layer.name if layer else None,
      'loop_count': len(obj.data.loops),
      'outside_loops': 0,
      'nonfinite_loops': 0,
      'zero_area_faces': 0 if valid else len(obj.data.polygons),
    }

  def inspect_uv_map(self, obj, **_kwargs):
    self.calls.append(('inspect_uv_map', obj.name))
    return self._inspect(obj)

  def poll_unwrap(self, job, **_kwargs):
    self.calls.append(('poll_unwrap', job.get('job_id')))
    if job.get('service_id') != self.SERVICE_ID:
      raise RuntimeError('provider rejected the job service id')
    if job.get('api_version') != self.API_VERSION:
      raise RuntimeError('provider rejected the job API version')
    if self.running:
      return self._receipt(
        'poll_unwrap',
        {'complete': False, 'job_id': job.get('job_id')},
        'RUNNING',
      )

    reports = []
    for pinned in job.get('objects') or ():
      obj = bpy.data.objects.get(pinned.get('name', ''))
      if obj is None or int(obj.as_pointer()) != int(pinned.get('pointer', -1)):
        raise RuntimeError('provider detected a replaced UVgami object')
      expected = int(pinned.get('polygon_count', -1))
      actual = len(obj.data.polygons)
      if actual != expected:
        raise RuntimeError(
          f'provider detected topology changed ({expected} -> {actual})'
        )
      report = self._inspect(obj)
      if not report['valid']:
        raise RuntimeError('provider detected an invalid UV map')
      reports.append({'object': obj.name, 'polygon_count': actual, **report})
    return self._receipt(
      'poll_unwrap',
      {
        'complete': True,
        'job_id': job.get('job_id'),
        'profile': job.get('profile'),
        'settings': dict(job.get('settings') or {}),
        'objects': reports,
        'manager_summary': list(self.manager_summary),
      },
      'SUCCESS',
    )


@unittest.skipUnless(bpy is not None, 'requires Blender Python')
class MeshyUVgamiBlenderTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    import addon_utils
    addon_utils.enable('substance_tools', default_set=False, persistent=False)
    from substance_tools import meshy_pipeline
    cls.pipeline = meshy_pipeline

  @classmethod
  def tearDownClass(cls):
    import addon_utils
    addon_utils.disable('substance_tools', default_set=False)

  def setUp(self):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
      bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    if self.pipeline.STATE_PROPERTY in bpy.context.scene:
      del bpy.context.scene[self.pipeline.STATE_PROPERTY]
    self.provider = FakeUVgamiProvider()
    self.original_resolver = self.pipeline._resolve_uvgami_workflow_api
    self.pipeline._resolve_uvgami_workflow_api = lambda: self.provider.api

  def tearDown(self):
    self.pipeline._resolve_uvgami_workflow_api = self.original_resolver

  def _low_with_valid_uv(self):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    low = bpy.context.object
    low.name = 'UVgamiAsset_low'
    mesh = low.data
    layer = mesh.uv_layers.active or mesh.uv_layers.new(name='LowUV')
    corners = ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9))
    for polygon in mesh.polygons:
      for corner_index, loop_index in enumerate(polygon.loop_indices):
        layer.data[loop_index].uv = corners[corner_index % 4]
    layer.active_render = True
    return low

  def _low_created_state(self, low):
    return {
      'schema_version': self.pipeline.STATE_SCHEMA_VERSION,
      'stage': 'LOW_CREATED',
      'asset_base': 'UVgamiAsset',
      'analysis': {'target_quads': 5000},
      'low': {
        'high_object': 'UVgamiAsset_high',
        'low_object': low.name,
        'actual_low_polygons': len(low.data.polygons),
      },
      'checkpoints': {},
    }

  def test_async_running_to_success_stores_provider_receipt(self):
    low = self._low_with_valid_uv()
    mesh_pointer = low.data.as_pointer()
    polygon_count = len(low.data.polygons)
    state = self._low_created_state(low)
    settings = bpy.context.scene.substance_tools_meshy_pipeline
    settings.uv_resolution = 2048
    settings.uv_margin_pixels = 8
    original_ui = dict(self.provider.ui_state)

    state, status, request = self.pipeline._continue_uvgami_low_uv(
      bpy.context.scene,
      state,
    )
    self.assertEqual(status, 'RUNNING')
    self.assertEqual(state['stage'], 'UVGAMI_RUNNING')
    self.assertEqual(request['polygon_count'], polygon_count)
    self.assertEqual(request['method'], self.pipeline.UV_METHOD_VERSION)
    self.assertEqual(request['provider_service_id'], 'uvgami.unwrap')
    self.assertEqual(request['provider_api_version'], 1)
    self.assertEqual(request['provider_job']['service_id'], 'uvgami.unwrap')
    self.assertEqual(self.provider.ui_state, original_ui)
    self.assertEqual(self.provider.applied_profiles[-1]['engine'], 'OPTCUTS')
    self.assertTrue(self.provider.applied_profiles[-1]['use_hard_surface'])
    self.assertAlmostEqual(
      self.provider.applied_profiles[-1]['margin'],
      8 / 2048,
      places=9,
    )

    state, status, receipt = self.pipeline._continue_uvgami_low_uv(
      bpy.context.scene,
      state,
    )
    self.assertEqual(status, 'RUNNING')
    self.assertIsNone(receipt)
    self.assertEqual(state['stage'], 'UVGAMI_RUNNING')

    self.provider.running = False
    self.provider.manager_summary = ['UV unwrap complete!']
    state, status, receipt = self.pipeline._continue_uvgami_low_uv(
      bpy.context.scene,
      state,
    )
    self.assertEqual(status, 'COMPLETE')
    self.assertEqual(state['stage'], 'UV_READY')
    self.assertTrue(receipt['valid'])
    self.assertEqual(receipt['method'], self.pipeline.UV_METHOD_VERSION)
    self.assertEqual(receipt['polygon_count'], polygon_count)
    self.assertEqual(receipt['provider_service_id'], 'uvgami.unwrap')
    self.assertEqual(receipt['provider_api_version'], 1)
    self.assertEqual(receipt['provider_receipt']['status'], 'SUCCESS')
    self.assertEqual(low.data.as_pointer(), mesh_pointer)
    self.assertEqual(len(low.data.polygons), polygon_count)
    stored = json.loads(low[self.pipeline.LOW_UV_RECEIPT_PROPERTY])
    self.assertEqual(stored['provider_receipt']['service_id'], 'uvgami.unwrap')
    self.assertEqual(stored['provider_receipt']['api_version'], 1)
    self.assertEqual(
      state['low']['uv']['provider_receipt']['operation'],
      'poll_unwrap',
    )

  def test_preflight_and_uv_inspection_are_provider_owned(self):
    low = self._low_with_valid_uv()
    original_ui = dict(self.provider.ui_state)
    api = self.pipeline._resolve_uvgami_workflow_api()

    preflight = self.pipeline._call_uvgami(
      api,
      'preflight_unwrap',
      [low],
      scene=bpy.context.scene,
      resolution=4096,
      margin_pixels=16,
    )
    self.assertEqual(preflight['status'], 'READY')
    self.assertEqual(preflight['service_id'], 'uvgami.unwrap')
    self.assertEqual(preflight['api_version'], 1)
    self.assertEqual(self.provider.ui_state, original_ui)
    self.assertEqual(self.provider.applied_profiles[-1]['resolution'], 4096)

    report = self.pipeline.validate_low_uv(low)
    self.assertTrue(report['valid'])
    self.assertIn(('inspect_uv_map', low.name), self.provider.calls)

  def test_provider_rejects_topology_change_from_pinned_job(self):
    low = self._low_with_valid_uv()
    pending = self.pipeline.launch_uvgami_low_uv(
      bpy.context.scene,
      low,
      resolution=1024,
      margin_pixels=4,
    )
    changed_mesh = bpy.data.meshes.new('UVgamiTopologyChanged')
    changed_mesh.from_pydata(
      ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0)),
      (),
      ((0, 1, 2),),
    )
    low.data = changed_mesh
    self.provider.running = False

    with self.assertRaisesRegex(
      self.pipeline.MeshyPipelineError,
      'provider detected topology changed',
    ):
      self.pipeline.confirm_uvgami_low_uv(
        bpy.context.scene,
        low,
        pending,
      )
    self.assertEqual(self.provider.calls[-1][0], 'poll_unwrap')

  def test_legacy_request_migrates_to_provider_job_before_poll(self):
    low = self._low_with_valid_uv()
    pending = {
      'method': self.pipeline.LEGACY_UV_METHOD_VERSION,
      'status': 'RUNNING',
      'low_object': low.name,
      'low_object_pointer': int(low.as_pointer()),
      'polygon_count': len(low.data.polygons),
      'settings': {
        'resolution': 2048,
        'margin_pixels': 8,
        'margin': 8 / 2048,
      },
    }
    state = self._low_created_state(low)
    state['low']['uvgami'] = pending
    self.provider.running = True

    state, status, receipt = self.pipeline._continue_uvgami_low_uv(
      bpy.context.scene,
      state,
    )
    self.assertEqual(status, 'RUNNING')
    self.assertIsNone(receipt)
    self.assertEqual(state['stage'], 'UVGAMI_RUNNING')
    self.assertEqual(
      self.pipeline.load_pipeline_state(bpy.context.scene)['stage'],
      'UVGAMI_RUNNING',
    )
    self.assertEqual(
      state['checkpoints']['UVGAMI_RUNNING']['low_object'],
      low.name,
    )

    self.provider.running = False
    state, status, receipt = self.pipeline._continue_uvgami_low_uv(
      bpy.context.scene,
      state,
    )
    self.assertEqual(status, 'COMPLETE')
    self.assertTrue(receipt['valid'])
    self.assertEqual(receipt['provider_receipt']['service_id'], 'uvgami.unwrap')
    self.assertEqual(state['stage'], 'UV_READY')

  def test_finalize_operator_reentry_polls_the_provider(self):
    low = self._low_with_valid_uv()
    state = self._low_created_state(low)
    self.pipeline.store_pipeline_state(bpy.context.scene, state)
    original_refresh = self.pipeline._refresh_recorded_low_export_unit
    self.pipeline._refresh_recorded_low_export_unit = (
      lambda _scene, current, **_kwargs: (
        (current.get('low') or {}).get('ue_unique_export_sync') or {},
        False,
      )
    )
    try:
      self.assertEqual(bpy.ops.st.finalize_meshy_retopo(), {'FINISHED'})
      state = self.pipeline.load_pipeline_state(bpy.context.scene)
      self.assertEqual(state['stage'], 'UVGAMI_RUNNING')
      self.assertEqual(state['low']['uvgami']['status'], 'RUNNING')

      self.assertEqual(bpy.ops.st.finalize_meshy_retopo(), {'FINISHED'})
      self.assertEqual(
        self.pipeline.load_pipeline_state(bpy.context.scene)['stage'],
        'UVGAMI_RUNNING',
      )

      self.provider.running = False
      self.assertEqual(bpy.ops.st.finalize_meshy_retopo(), {'FINISHED'})
      self.assertEqual(
        self.pipeline.load_pipeline_state(bpy.context.scene)['stage'],
        'UV_READY',
      )
      operations = [call[0] for call in self.provider.calls]
      self.assertEqual(operations.count('begin_unwrap'), 1)
      self.assertEqual(operations.count('poll_unwrap'), 2)
    finally:
      self.pipeline._refresh_recorded_low_export_unit = original_refresh

  def test_finalize_qr_ready_wraps_adoption_then_starts_uv(self):
    low = self._low_with_valid_uv()
    state = self._low_created_state(low)
    state['stage'] = 'QR_READY'
    state.pop('low')
    self.pipeline.store_pipeline_state(bpy.context.scene, state)
    calls = []
    original_adopt_stage = self.pipeline._adopt_retopology_pair_stage

    def adopt_stage(context):
      calls.append('adopt')
      current = self.pipeline.load_pipeline_state(context.scene)
      receipt = {
        'high_object': 'UVgamiAsset_high',
        'low_object': low.name,
        'actual_low_polygons': len(low.data.polygons),
      }
      current['low'] = receipt
      current = self.pipeline.advance_pipeline_state(
        current,
        'LOW_CREATED',
        receipt,
      )
      self.pipeline.store_pipeline_state(context.scene, current)
      return current, receipt, True

    self.pipeline._adopt_retopology_pair_stage = adopt_stage
    try:
      self.assertEqual(bpy.ops.st.finalize_meshy_retopo(), {'FINISHED'})
    finally:
      self.pipeline._adopt_retopology_pair_stage = original_adopt_stage

    observed = self.pipeline.load_pipeline_state(bpy.context.scene)
    self.assertEqual(calls, ['adopt'])
    self.assertEqual(observed['stage'], 'UVGAMI_RUNNING')
    operations = [call[0] for call in self.provider.calls]
    self.assertEqual(operations.count('begin_unwrap'), 1)
    self.assertNotIn('preflight_unwrap', operations)

  def test_incompatible_provider_begin_receipt_is_rejected(self):
    low = self._low_with_valid_uv()
    self.provider.receipt_api_version = 2
    with self.assertRaisesRegex(
      self.pipeline.MeshyPipelineError,
      'invalid begin receipt',
    ):
      self.pipeline.launch_uvgami_low_uv(
        bpy.context.scene,
        low,
        resolution=2048,
        margin_pixels=8,
      )


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

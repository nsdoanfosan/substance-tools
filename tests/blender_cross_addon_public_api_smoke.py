"""Factory-Blender smoke for Substance Tools -> UE Unique public APIs.

Run with an isolated BLENDER_USER_CONFIG and ``--factory-startup``.  This test
never saves preferences.
"""

import unittest
from unittest import mock

import addon_utils
import bpy


class CrossAddonPublicApiSmokeTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    addon_utils.enable('send2ue', default_set=False, persistent=False)
    addon_utils.enable(
      'ue_unique_export_names_addon',
      default_set=False,
      persistent=False,
    )
    addon_utils.enable(
      'quad_remesher_workflow_addon',
      default_set=False,
      persistent=False,
    )
    addon_utils.enable('substance_tools', default_set=False, persistent=False)

  @classmethod
  def tearDownClass(cls):
    addon_utils.disable('substance_tools', default_set=False)
    addon_utils.disable('quad_remesher_workflow_addon', default_set=False)
    addon_utils.disable('ue_unique_export_names_addon', default_set=False)
    addon_utils.disable('send2ue', default_set=False)

  def setUp(self):
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
      bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for name in ('Export', 'Baking'):
      collection = bpy.data.collections.get(name)
      if collection is not None:
        bpy.data.collections.remove(collection)

  def _state_and_topology(self, source, result, asset_base, stable_id):
    from quad_remesher_workflow_addon import api as qr_api
    from substance_tools import api, meshy_pipeline

    request = {
      'api_version': qr_api.API_VERSION,
      'preexisting_mesh_objects': [source.name],
    }
    topology = qr_api.validate_result(
      source,
      result,
      request,
      scene=bpy.context.scene,
      asset_base=asset_base,
    )
    state = {
      'stage': 'QR_READY',
      'asset_base': asset_base,
      'source': {
        'object_name': source.name,
        'mesh_name': source.data.name,
        'stable_id': stable_id,
        'content_signature': meshy_pipeline.mesh_object_content_signature(
          source,
          bpy.context.scene,
        ),
      },
      'qr': request,
    }
    return state, topology

  def test_adoption_names_classifies_and_calls_export_owner_once(self):
    from substance_tools import api
    from ue_unique_export_names_addon import api as ue_api

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'PublicApiAsset'
    source.location = (1.25, -2.5, 3.75)
    material = bpy.data.materials.new('M_PublicApiAsset')
    source.data.materials.append(material)

    result = source.copy()
    result.data = source.data.copy()
    result.name = 'RetopoPublicApiAsset'
    bpy.context.scene.collection.objects.link(result)
    result.location = source.location
    bpy.context.view_layer.update()
    original_world_matrix = result.matrix_world.copy()

    state, topology = self._state_and_topology(
      source,
      result,
      'PublicApiAsset',
      'factory-smoke-stable-id',
    )
    immediate_extension = (
      bpy.context.scene.send2ue.extensions.use_immediate_parent_name
    )
    immediate_extension.use_immediate_parent_name = True

    receipt = api.adopt_retopology_pair(
      source,
      result,
      state,
      topology,
      scene=bpy.context.scene,
    )

    self.assertEqual(receipt['api_version'], 1)
    self.assertEqual(receipt['service_id'], 'substance.painter_transfer')
    self.assertEqual(receipt['operation'], 'adopt_retopology_pair')
    self.assertEqual(receipt['status'], 'SUCCESS')
    self.assertEqual(source.name, 'PublicApiAsset_high')
    self.assertEqual(result.name, 'PublicApiAsset_low')

    baking = bpy.data.collections.get('Baking')
    self.assertIsNotNone(baking)
    low_collection = baking.children.get('low')
    high_collection = baking.children.get('high')
    self.assertIsNotNone(low_collection)
    self.assertIsNotNone(high_collection)
    self.assertIs(low_collection.objects.get(result.name), result)
    self.assertIs(high_collection.objects.get(source.name), source)
    self.assertIsNone(low_collection.objects.get(source.name))
    self.assertIsNone(high_collection.objects.get(result.name))

    export = bpy.data.collections.get('Export')
    self.assertIsNotNone(export)
    export_root = bpy.data.objects.get('PublicApiAsset')
    self.assertIsNotNone(export_root)
    self.assertEqual(export_root.type, 'EMPTY')
    self.assertIs(result.parent, export_root)
    self.assertEqual(result.name, 'PublicApiAsset_low')
    self.assertIs(export.objects.get(export_root.name), export_root)
    self.assertIs(export.objects.get(result.name), result)
    self.assertIsNone(export.objects.get(source.name))
    sync = receipt['ue_unique_export_sync']
    self.assertTrue(sync['available'])
    self.assertTrue(sync['synced'])
    self.assertEqual(sync['api_version'], 2)
    self.assertEqual(sync['operation'], 'ensure_painter_low_export_unit')
    self.assertEqual(sync['unit_status'], 'STATIC_EMPTY_READY')
    self.assertEqual(sync['export_root'], 'PublicApiAsset')
    self.assertTrue(sync['root_name_matches'])
    self.assertTrue(sync['created_empty'])
    self.assertTrue(sync['preserved_world_transform'])
    self.assertEqual(result.matrix_world, original_world_matrix)
    self.assertEqual(sync['combine_assets']['value'], 'child_meshes')
    immediate = sync['combine_assets']['use_immediate_parent_name']
    self.assertTrue(immediate['available'])
    self.assertTrue(immediate['previous'])
    self.assertFalse(immediate['value'])
    self.assertTrue(immediate['changed'])
    self.assertEqual(
      bpy.context.scene.send2ue.extensions.combine_assets.combine,
      'child_meshes',
    )
    self.assertFalse(
      bpy.context.scene.send2ue.extensions
      .use_immediate_parent_name.use_immediate_parent_name
    )
    self.assertEqual(sync['desired'], 2)

    # Exercise Send2UE's native extension hook, not merely its property.  The
    # Painter child keeps ``_low`` while the actual export paths use the Empty.
    send2ue_wm = bpy.context.window_manager.send2ue
    asset_id = 'public-api-empty-name-smoke'
    send2ue_wm.asset_id = asset_id
    send2ue_wm.asset_data[asset_id] = {
      '_mesh_object_name': result.name,
      'file_path': r'C:\Temp\PublicApiAsset_low.fbx',
      'asset_folder': '/Game/Props/',
      'asset_path': '/Game/Props/PublicApiAsset_low',
    }
    bpy.context.scene.send2ue.extensions.combine_assets.pre_mesh_export(
      send2ue_wm.asset_data[asset_id],
      bpy.context.scene.send2ue,
    )
    send2ue_asset = send2ue_wm.asset_data[asset_id]
    self.assertTrue(send2ue_asset['file_path'].endswith('PublicApiAsset.fbx'))
    self.assertEqual(send2ue_asset['asset_path'], '/Game/Props/PublicApiAsset')
    self.assertEqual(send2ue_asset['empty_object_name'], 'PublicApiAsset')

    second = ue_api.ensure_painter_low_export_unit(
      result,
      'PublicApiAsset',
      scene=bpy.context.scene,
    )
    self.assertFalse(second['created_empty'])
    self.assertFalse(second['parented'])
    self.assertEqual(second['unit_status'], 'EXISTING_EMPTY_READY')
    self.assertEqual(second['export_root'], 'PublicApiAsset')
    self.assertEqual(
      len([obj for obj in bpy.data.objects if obj.name == 'PublicApiAsset']),
      1,
    )

  def test_staged_adoption_operator_stops_before_uvgami(self):
    from substance_tools import meshy_pipeline

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'StagedAdoptionAsset'
    material = bpy.data.materials.new('M_StagedAdoptionAsset')
    source.data.materials.append(material)

    result = source.copy()
    result.data = source.data.copy()
    result.name = 'Retopo_StagedAdoptionAsset'
    bpy.context.scene.collection.objects.link(result)
    state, _ = self._state_and_topology(
      source,
      result,
      'StagedAdoptionAsset',
      'staged-adoption-source',
    )
    state.update({
      'analysis': {'target_quads': 5000},
      'archive': {'source_original': {'test_receipt': True}},
      'checkpoints': {},
    })
    meshy_pipeline.store_pipeline_state(bpy.context.scene, state)
    bpy.ops.object.select_all(action='DESELECT')
    result.select_set(True)
    bpy.context.view_layer.objects.active = result

    unexpected_uv_calls = []

    def fail_if_uvgami_is_resolved():
      unexpected_uv_calls.append('resolve')
      raise AssertionError('staged adoption must not resolve UVgami')

    with mock.patch.object(
      meshy_pipeline,
      'verify_source_archive_receipt',
      return_value={'verified': True},
    ), mock.patch.object(
      meshy_pipeline,
      '_validate_qr_result_via_owner',
      wraps=meshy_pipeline._validate_qr_result_via_owner,
    ) as validate_qr, mock.patch.object(
      meshy_pipeline,
      '_resolve_uvgami_workflow_api',
      side_effect=fail_if_uvgami_is_resolved,
    ):
      self.assertTrue(hasattr(bpy.ops.st, 'adopt_retopology_pair'))
      self.assertEqual(bpy.ops.st.adopt_retopology_pair(), {'FINISHED'})
      adopted = meshy_pipeline.load_pipeline_state(bpy.context.scene)
      self.assertEqual(adopted['stage'], 'LOW_CREATED')
      self.assertEqual(adopted['low']['high_object'], 'StagedAdoptionAsset_high')
      self.assertEqual(adopted['low']['low_object'], 'StagedAdoptionAsset_low')
      self.assertEqual(
        adopted['low']['topology_owner_receipt']['status'],
        'SUCCESS',
      )
      self.assertEqual(adopted['checkpoints']['LOW_CREATED'], adopted['low'])
      self.assertNotIn('uvgami', adopted['low'])
      self.assertNotIn('uv', adopted['low'])
      self.assertEqual(unexpected_uv_calls, [])
      validate_qr.assert_called_once()

      # Re-entry is an idempotent adoption check; it neither regresses the
      # checkpoint nor asks either topology or UV owners to run again.
      self.assertEqual(bpy.ops.st.adopt_retopology_pair(), {'FINISHED'})
      self.assertEqual(
        meshy_pipeline.load_pipeline_state(bpy.context.scene)['stage'],
        'LOW_CREATED',
      )
      self.assertEqual(unexpected_uv_calls, [])
      validate_qr.assert_called_once()

    baking = bpy.data.collections.get('Baking')
    self.assertIsNotNone(baking)
    self.assertIs(
      baking.children['high'].objects.get('StagedAdoptionAsset_high'),
      source,
    )
    self.assertIs(
      baking.children['low'].objects.get('StagedAdoptionAsset_low'),
      result,
    )

  def test_missing_optional_export_addon_is_reported_without_failing_adoption(self):
    from substance_tools import api, meshy_pipeline

    addon_utils.disable('ue_unique_export_names_addon', default_set=False)
    export = bpy.data.collections.get('Export')
    if export is not None:
      bpy.data.collections.remove(export)

    bpy.ops.mesh.primitive_cube_add(size=1.0)
    source = bpy.context.object
    source.name = 'OptionalApiAsset'
    material = bpy.data.materials.new('M_OptionalApiAsset')
    source.data.materials.append(material)
    result = source.copy()
    result.data = source.data.copy()
    result.name = 'RetopoOptionalApiAsset'
    bpy.context.scene.collection.objects.link(result)

    state, topology = self._state_and_topology(
      source,
      result,
      'OptionalApiAsset',
      'optional-api-smoke-stable-id',
    )

    real_pipeline_import_module = meshy_pipeline.importlib.import_module

    def without_ue_unique(module_name, *args, **kwargs):
      if module_name == 'ue_unique_export_names_addon.api':
        raise ModuleNotFoundError(module_name, name=module_name)
      return real_pipeline_import_module(module_name, *args, **kwargs)

    meshy_pipeline.importlib.import_module = without_ue_unique
    try:
      receipt = api.adopt_retopology_pair(
        source,
        result,
        state,
        topology,
        scene=bpy.context.scene,
      )
      self.assertEqual(receipt['status'], 'SUCCESS')
      self.assertEqual(source.name, 'OptionalApiAsset_high')
      self.assertEqual(result.name, 'OptionalApiAsset_low')
      sync = receipt['ue_unique_export_sync']
      self.assertFalse(sync['available'])
      self.assertFalse(sync['synced'])
      self.assertEqual(sync['reason'], 'optional_addon_unavailable')
      self.assertEqual(sync['expected_api_version'], 2)
      self.assertIsNone(
        bpy.data.collections.get('Export'),
        'Substance Tools must not create Export when the owner API is unavailable',
      )
    finally:
      meshy_pipeline.importlib.import_module = real_pipeline_import_module
      addon_utils.enable(
        'ue_unique_export_names_addon',
        default_set=False,
        persistent=False,
      )

  def test_direct_operator_path_rejects_api_version_before_mutation(self):
    from substance_tools import api, meshy_pipeline

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'VersionGuardSource'
    material = bpy.data.materials.new('M_VersionGuardAsset')
    source.data.materials.append(material)
    result = source.copy()
    result.data = source.data.copy()
    result.name = 'Retopo_VersionGuardSource'
    bpy.context.scene.collection.objects.link(result)
    state, topology = self._state_and_topology(
      source,
      result,
      'VersionGuardAsset',
      'version-guard-source',
    )
    before = {
      'source_name': source.name,
      'result_name': result.name,
      'source_collections': tuple(source.users_collection),
      'result_collections': tuple(result.users_collection),
      'objects': tuple(obj.as_pointer() for obj in bpy.data.objects),
    }
    incompatible = {
      'owner': 'ue-unique-export-names-addon',
      'service_id': 'unreal-handoff.painter-low-export',
      'module': 'ue_unique_export_names_addon.api',
      'getter': 'get_painter_low_export_api',
      'function': 'ensure_painter_low_export_unit',
      'version': 999,
    }
    with mock.patch.object(
      meshy_pipeline,
      'integration_api',
      return_value=incompatible,
    ):
      with self.assertRaisesRegex(
        api.IntegrationApiContractError,
        'incompatible',
      ):
        api.adopt_retopology_pair(
          source,
          result,
          state,
          topology,
          scene=bpy.context.scene,
        )
    after = {
      'source_name': source.name,
      'result_name': result.name,
      'source_collections': tuple(source.users_collection),
      'result_collections': tuple(result.users_collection),
      'objects': tuple(obj.as_pointer() for obj in bpy.data.objects),
    }
    self.assertEqual(after, before)
    self.assertIsNone(bpy.data.collections.get('Baking'))
    self.assertIsNone(bpy.data.collections.get('Export'))

  def test_installed_owner_collision_aborts_and_rolls_back_adoption(self):
    from substance_tools import api, meshy_pipeline

    collision = bpy.data.objects.new('CollisionAsset', None)
    bpy.context.scene.collection.objects.link(collision)
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'CollisionSource'
    material = bpy.data.materials.new('M_CollisionAsset')
    source.data.materials.append(material)
    result = source.copy()
    original_result_data = source.data.copy()
    result.data = original_result_data
    result.name = 'Retopo_CollisionSource'
    bpy.context.scene.collection.objects.link(result)
    result.data.materials.clear()
    result.data.materials.append(material)
    state, topology = self._state_and_topology(
      source,
      result,
      'CollisionAsset',
      'collision-source',
    )
    combine = bpy.context.scene.send2ue.extensions.combine_assets
    immediate = (
      bpy.context.scene.send2ue.extensions.use_immediate_parent_name
    )
    combine.combine = 'off'
    immediate.use_immediate_parent_name = True

    with self.assertRaisesRegex(
      meshy_pipeline.MeshyPipelineError,
      'export-unit preparation failed',
    ):
      api.adopt_retopology_pair(
        source,
        result,
        state,
        topology,
        scene=bpy.context.scene,
      )

    self.assertEqual(source.name, 'CollisionSource')
    self.assertEqual(result.name, 'Retopo_CollisionSource')
    self.assertIs(result.data, original_result_data)
    self.assertIs(source.material_slots[0].material, material)
    self.assertIs(result.material_slots[0].material, material)
    self.assertIs(bpy.data.objects.get('CollisionAsset'), collision)
    self.assertIsNone(bpy.data.objects.get('CollisionAsset.001'))
    self.assertIsNone(bpy.data.collections.get('Baking'))
    self.assertIsNone(bpy.data.collections.get('Export'))
    self.assertEqual(combine.combine, 'off')
    self.assertTrue(immediate.use_immediate_parent_name)

  def test_send2ue_late_enable_and_verify_time_drift_are_retried(self):
    from substance_tools import api, meshy_pipeline

    addon_utils.disable('send2ue', default_set=False)
    try:
      bpy.ops.mesh.primitive_cube_add(size=2.0)
      source = bpy.context.object
      source.name = 'LateSend2UEAsset'
      material = bpy.data.materials.new('M_LateSend2UEAsset')
      source.data.materials.append(material)
      result = source.copy()
      result.data = source.data.copy()
      result.name = 'Retopo_LateSend2UEAsset'
      bpy.context.scene.collection.objects.link(result)
      state, topology = self._state_and_topology(
        source,
        result,
        'LateSend2UEAsset',
        'late-send2ue-source',
      )
      adoption = api.adopt_retopology_pair(
        source,
        result,
        state,
        topology,
        scene=bpy.context.scene,
      )
      pending = adoption['ue_unique_export_sync']
      self.assertTrue(pending['synced'])
      self.assertFalse(pending['handoff_ready'])
      self.assertEqual(pending['status'], 'PENDING_SEND2UE')
      state['stage'] = 'LOW_CREATED'
      state['low'] = adoption

      addon_utils.enable('send2ue', default_set=False, persistent=False)
      combine = bpy.context.scene.send2ue.extensions.combine_assets
      immediate = (
        bpy.context.scene.send2ue.extensions.use_immediate_parent_name
      )
      combine.combine = 'off'
      immediate.use_immediate_parent_name = True
      refreshed, changed = meshy_pipeline._refresh_recorded_low_export_unit(
        bpy.context.scene,
        state,
      )
      self.assertTrue(changed)
      self.assertTrue(refreshed['handoff_ready'])
      self.assertEqual(refreshed['status'], 'SUCCESS')
      self.assertEqual(combine.combine, 'child_meshes')
      self.assertFalse(immediate.use_immediate_parent_name)

      # Final Verify uses force=True, so a later scene-wide setting change
      # cannot be hidden by the previously successful receipt.
      combine.combine = 'off'
      immediate.use_immediate_parent_name = True
      refreshed, _ = meshy_pipeline._refresh_recorded_low_export_unit(
        bpy.context.scene,
        state,
        force=True,
      )
      self.assertTrue(refreshed['handoff_ready'])
      self.assertEqual(combine.combine, 'child_meshes')
      self.assertFalse(immediate.use_immediate_parent_name)
    finally:
      addon_utils.enable('send2ue', default_set=False, persistent=False)


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

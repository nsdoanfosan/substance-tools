"""Isolated Blender smoke tests for the Meshy preparation module.

Regular CPython test discovery skips the Blender-only case.  Run it with a
factory-startup Blender process; the test never saves preferences or a blend.
"""

import json
import unittest

try:
  import bpy
except ModuleNotFoundError:
  bpy = None


@unittest.skipUnless(bpy is not None, 'requires Blender Python')
class MeshyPipelineBlenderTests(unittest.TestCase):

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
    bpy.ops.object.mode_set(mode='OBJECT') if bpy.context.object and bpy.context.object.mode != 'OBJECT' else None
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for material in list(bpy.data.materials):
      bpy.data.materials.remove(material)
    if self.pipeline.STATE_PROPERTY in bpy.context.scene:
      del bpy.context.scene[self.pipeline.STATE_PROPERTY]
    if self.pipeline.LEGACY_STATE_PROPERTY in bpy.context.scene:
      del bpy.context.scene[self.pipeline.LEGACY_STATE_PROPERTY]

  def test_v1_state_is_visible_only_as_read_only_legacy(self):
    legacy = {
      'schema_version': 1,
      'stage': 'QR_READY',
      'asset_base': 'LegacyAsset',
      'analysis': {'target_quads': 25000},
    }
    bpy.context.scene[self.pipeline.LEGACY_STATE_PROPERTY] = json.dumps(legacy)
    with self.assertRaisesRegex(self.pipeline.MeshyPipelineError, 'read-only'):
      self.pipeline.load_pipeline_state(bpy.context.scene)
    observed = self.pipeline.load_pipeline_state(
      bpy.context.scene,
      allow_legacy=True,
    )
    self.assertTrue(observed['_legacy_read_only'])
    self.assertEqual(observed['stage'], 'QR_READY')
    self.assertNotIn(self.pipeline.STATE_PROPERTY, bpy.context.scene)

  def test_name_and_override_helpers(self):
    self.assertEqual(
      self.pipeline.object_base_name('Retopo_SM_Prop_Huya_low.001'),
      'SM_Prop_Huya',
    )
    self.assertEqual(self.pipeline.validate_target_override(25000), 25000)
    with self.assertRaises(self.pipeline.MeshyPipelineError):
      self.pipeline.validate_target_override(25500)

  def test_painter_defaults_match_meshy_contract(self):
    settings = bpy.context.scene.substance_tools_baking
    self.assertEqual(settings.antialiasing, 'X2')
    self.assertEqual(settings.match, 'BY_MESH_NAME')
    self.assertEqual(settings.id_source, 'MATERIAL_COLOR')
    settings.antialiasing = 'NONE'
    settings.match = 'ALWAYS'
    settings.id_source = 'FACE_SETS'
    self.pipeline.configure_meshy_painter_defaults(bpy.context.scene)
    self.assertEqual(settings.antialiasing, 'X2')
    self.assertEqual(settings.match, 'BY_MESH_NAME')
    self.assertEqual(settings.id_source, 'MATERIAL_COLOR')

  def test_final_roles_follow_optional_source_package(self):
    state = {
      'painter_package': {
        'painter_texture_sets': ['ColorOnly', 'ExtraOnly', 'NormalOnly'],
        'maps': {
          'ColorOnly': {'BaseColor': 'color.png'},
          'ExtraOnly': {
            'Extra': 'extra.png',
            'ExtraR': 'r.png',
            'Roughness': 'g.png',
            'Metallic': 'b.png',
          },
          'NormalOnly': {'Normal': 'normal.png'},
        },
      },
    }
    roles = self.pipeline.required_canonical_roles_from_state(state)
    self.assertEqual(roles['ColorOnly'], {'Color'})
    self.assertEqual(roles['ExtraOnly'], {'Extra'})
    self.assertEqual(roles['NormalOnly'], {'Normal'})

    empty_state = {
      'painter_package': {
        'painter_texture_sets': ['NoSourceMaps'],
        'maps': {'NoSourceMaps': {}},
      },
    }
    self.assertEqual(
      self.pipeline.required_canonical_roles_from_state(empty_state),
      {'NoSourceMaps': set()},
    )

    state['painter_package']['maps']['ExtraOnly'].pop('Metallic')
    with self.assertRaisesRegex(self.pipeline.MeshyPipelineError, 'incomplete'):
      self.pipeline.required_canonical_roles_from_state(state)

  def test_analyze_reports_evaluated_tris_and_world_bbox(self):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'SM_Test_Source'
    analysis = self.pipeline.analyze_mesh_object(source, bpy.context.scene)
    self.assertEqual(analysis['evaluated_triangles'], 12)
    self.assertEqual(analysis['target_mode'], 'SKIP_ALREADY_LOW')
    self.assertAlmostEqual(analysis['bbox_diagonal_m'], 12 ** 0.5, places=6)
    bpy.context.view_layer.objects.active = source
    source.select_set(True)
    self.assertEqual(bpy.ops.st.analyze_meshy_source(), {'FINISHED'})
    self.assertNotIn(self.pipeline.STATE_PROPERTY, bpy.context.scene)

  def test_adopt_rejects_wrong_scale_before_mutation(self):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'Source'
    material = bpy.data.materials.new('M_TestAsset')
    material.use_nodes = True
    source.data.materials.append(material)
    result = source.copy()
    result.data = source.data.copy()
    bpy.context.scene.collection.objects.link(result)
    result.name = 'Retopo_Source'
    result.scale = (0.5, 0.5, 0.5)
    state = {
      'schema_version': self.pipeline.STATE_SCHEMA_VERSION,
      'stage': 'QR_READY',
      'asset_base': 'TestAsset',
      'analysis': {'target_quads': 5000},
      'source': {
        'object_name': source.name,
        'mesh_name': source.data.name,
        'stable_id': 'scaled-source',
        'content_signature': self.pipeline.mesh_object_content_signature(
          source,
          bpy.context.scene,
        ),
      },
      'qr': {'preexisting_mesh_objects': [source.name]},
      'checkpoints': {},
    }
    with self.assertRaises(self.pipeline.MeshyPipelineError):
      self.pipeline.adopt_qr_result(
        bpy.context.scene,
        source,
        result,
        state,
        resolution=2048,
        margin_pixels=8,
      )
    self.assertEqual(source.name, 'Source')
    self.assertEqual(result.name, 'Retopo_Source')

  def test_late_uv_rebuild_is_refused_without_state_or_uv_change(self):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    low = bpy.context.object
    low.name = 'TestAsset_low'
    before_uv = [tuple(loop.uv) for loop in low.data.uv_layers.active.data]
    state = {
      'schema_version': self.pipeline.STATE_SCHEMA_VERSION,
      'stage': 'BAKE_BASELINE_ARCHIVED',
      'asset_base': 'TestAsset',
      'analysis': {'target_quads': 5000},
      'source': {},
      'low': {'low_object': low.name},
      'checkpoints': {},
    }
    self.pipeline.store_pipeline_state(bpy.context.scene, state)
    state_before = bpy.context.scene[self.pipeline.STATE_PROPERTY]
    with self.assertRaisesRegex(RuntimeError, 'cannot be rebuilt'):
      bpy.ops.st.prepare_meshy_low_uv(force_rebuild=True)
    self.assertEqual(bpy.context.scene[self.pipeline.STATE_PROPERTY], state_before)
    self.assertEqual(
      [tuple(loop.uv) for loop in low.data.uv_layers.active.data],
      before_uv,
    )

  def test_adopt_result_isolates_names_materials_and_baking_collections(self):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'Source'
    material = bpy.data.materials.new('M_TestAsset')
    material.use_nodes = True
    source.data.materials.append(material)

    result = source.copy()
    result.data = source.data.copy()
    bpy.context.scene.collection.objects.link(result)
    result.name = 'Retopo_Source'
    result.data.materials.clear()
    result.data.materials.append(material)

    source_id = 'test-source-id'
    source[self.pipeline.SOURCE_ID_PROPERTY] = source_id
    state = {
      'schema_version': self.pipeline.STATE_SCHEMA_VERSION,
      'stage': 'QR_READY',
      'asset_base': 'TestAsset',
      'analysis': {'target_quads': 5000},
      'source': {
        'object_name': source.name,
        'mesh_name': source.data.name,
        'stable_id': source_id,
        'content_signature': self.pipeline.mesh_object_content_signature(
          source,
          bpy.context.scene,
        ),
      },
      'qr': {'preexisting_mesh_objects': [source.name]},
      'checkpoints': {},
    }
    self.pipeline.store_pipeline_state(bpy.context.scene, state)
    bpy.ops.object.select_all(action='DESELECT')
    result.select_set(True)
    bpy.context.view_layer.objects.active = result

    receipt = self.pipeline.adopt_qr_result(
      bpy.context.scene,
      source,
      result,
      state,
      resolution=2048,
      margin_pixels=8,
    )

    self.assertEqual(source.name, 'TestAsset_high')
    self.assertEqual(result.name, 'TestAsset_low')
    self.assertIsNot(source.material_slots[0].material, result.material_slots[0].material)
    self.assertIsNot(
      source.material_slots[0].material.node_tree,
      result.material_slots[0].material.node_tree,
    )
    self.assertNotIn('uv', receipt)
    source_collections = {collection.name for collection in source.users_collection}
    result_collections = {collection.name for collection in result.users_collection}
    self.assertIn(self.pipeline.HIGH_COLLECTION, source_collections)
    self.assertNotIn(self.pipeline.LOW_COLLECTION, source_collections)
    self.assertIn(self.pipeline.LOW_COLLECTION, result_collections)
    self.assertNotIn(self.pipeline.HIGH_COLLECTION, result_collections)
    export_sync = receipt['ue_unique_export_sync']
    self.assertIn('available', export_sync)
    if export_sync['available']:
      self.assertEqual(export_sync['api_version'], 2)
      export_collection = bpy.data.collections.get('Export')
      self.assertIsNotNone(export_collection)
      self.assertIs(export_collection.objects.get(result.name), result)
      self.assertIsNone(export_collection.objects.get(source.name))
    recovered = self.pipeline.recover_committed_adoption(
      bpy.context.scene,
      state,
    )
    self.assertTrue(recovered['recovered_after_state_write_gap'])
    self.assertEqual(recovered['high_object'], source.name)
    self.assertEqual(recovered['low_object'], result.name)
    self.assertNotIn('content_signatures', recovered)
    self.assertNotIn('uv', recovered)


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

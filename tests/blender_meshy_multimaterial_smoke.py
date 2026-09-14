"""Factory-Blender smoke test for two independent Meshy Texture Sets.

Run only with ``--factory-startup`` and an isolated ``BLENDER_USER_CONFIG``.
The test enables the add-on with ``default_set=False`` and never saves user
preferences or invokes Quad Remesher.
"""

from array import array
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import addon_utils
import bpy


SOURCE_BASENAME = 'source.png'
TEXTURE_SETS = ('MultiSetA', 'MultiSetB')
SOURCE_VALUES = {
  'MultiSetA': {
    'Color': (0.15, 0.30, 0.45, 1.0),
    'Extra': (0.20, 0.35, 0.50, 1.0),
    'Normal': (0.50, 0.50, 1.00, 1.0),
  },
  'MultiSetB': {
    'Color': (0.75, 0.55, 0.25, 1.0),
    'Extra': (0.75, 0.65, 0.85, 1.0),
    'Normal': (0.65, 0.45, 0.95, 1.0),
  },
}
ROLE_PROPERTY_VALUES = {
  'Color': 'base_color',
  'Extra': 'metallic_roughness',
  'Normal': 'normal',
}


def _write_external_constant_image(root, texture_set, role, rgba):
  directory = Path(root) / 'source_inputs' / texture_set / role
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / SOURCE_BASENAME

  generated = bpy.data.images.new(
    f'__Write_{texture_set}_{role}',
    width=8,
    height=8,
    alpha=False,
  )
  generated.colorspace_settings.name = (
    'sRGB' if role == 'Color' else 'Non-Color'
  )
  generated.pixels.foreach_set(array('f', rgba) * 64)
  generated.filepath_raw = str(path)
  generated.file_format = 'PNG'
  generated.save()
  bpy.data.images.remove(generated)

  image = bpy.data.images.load(str(path), check_existing=False)
  image.name = f'{texture_set}_{role}_Source'
  image.colorspace_settings.name = 'sRGB' if role == 'Color' else 'Non-Color'
  image['_ue_unique_export_original_name'] = ROLE_PROPERTY_VALUES[role]
  return image, path


def _make_source_material(texture_set, images):
  material = bpy.data.materials.new(f'M_{texture_set}')
  material.use_nodes = True
  nodes = material.node_tree.nodes
  links = material.node_tree.links
  principled = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')

  color_node = nodes.new('ShaderNodeTexImage')
  color_node.name = f'{texture_set} Color Source'
  color_node.image = images['Color']
  links.new(color_node.outputs['Color'], principled.inputs['Base Color'])

  extra_node = nodes.new('ShaderNodeTexImage')
  extra_node.name = f'{texture_set} Extra Source'
  extra_node.image = images['Extra']
  separate = nodes.new('ShaderNodeSeparateColor')
  separate.name = f'{texture_set} Extra Channels'
  links.new(extra_node.outputs['Color'], separate.inputs['Color'])
  links.new(separate.outputs['Green'], principled.inputs['Roughness'])
  links.new(separate.outputs['Blue'], principled.inputs['Metallic'])

  normal_node = nodes.new('ShaderNodeTexImage')
  normal_node.name = f'{texture_set} Normal Source'
  normal_node.image = images['Normal']
  normal_map = nodes.new('ShaderNodeNormalMap')
  normal_map.name = f'{texture_set} Normal Map'
  normal_map.space = 'TANGENT'
  links.new(normal_node.outputs['Color'], normal_map.inputs['Color'])
  links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
  return material


def _assign_overlapping_valid_uv(mesh):
  uv = mesh.uv_layers.new(name='UVMap', do_init=False)
  corners = (
    (0.05, 0.05),
    (0.95, 0.05),
    (0.95, 0.95),
    (0.05, 0.95),
  )
  for polygon in mesh.polygons:
    if len(polygon.loop_indices) != 4:
      raise AssertionError('The smoke fixture expects quad cube faces')
    for coordinate, loop_index in zip(corners, polygon.loop_indices):
      uv.data[loop_index].uv = coordinate
  mesh.uv_layers.active = uv
  uv.active_render = True


def _pixel_receipt(path):
  image = bpy.data.images.load(str(path), check_existing=False)
  try:
    pixels = array('f', [0.0]) * len(image.pixels)
    image.pixels.foreach_get(pixels)
    rgb = [pixels[index] for index in range(len(pixels)) if index % 4 != 3]
    return {
      'digest': hashlib.sha256(pixels.tobytes()).hexdigest(),
      'maximum': max(rgb),
      'minimum': min(rgb),
    }
  finally:
    bpy.data.images.remove(image)


class MeshyMultiMaterialSmokeTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    bridge = addon_utils.enable(
      'quad_remesher_workflow_addon',
      default_set=False,
      persistent=False,
    )
    if bridge is None:
      raise RuntimeError('quad_remesher_workflow_addon could not be enabled')
    module = addon_utils.enable(
      'substance_tools',
      default_set=False,
      persistent=False,
    )
    if module is None:
      raise RuntimeError('substance_tools could not be enabled')

  @classmethod
  def tearDownClass(cls):
    addon_utils.disable('substance_tools', default_set=False)
    addon_utils.disable('quad_remesher_workflow_addon', default_set=False)

  def setUp(self):
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
      bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for material in list(bpy.data.materials):
      bpy.data.materials.remove(material)
    for image in list(bpy.data.images):
      bpy.data.images.remove(image)

  def _simple_adoption_pair(self, material_names):
    from substance_tools.meshy_pipeline import mesh_object_content_signature

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    source = bpy.context.object
    source.name = 'SM_NameContract'
    _assign_overlapping_valid_uv(source.data)
    for name in material_names:
      source.data.materials.append(bpy.data.materials.new(name))
    result = source.copy()
    result.data = source.data.copy()
    bpy.context.scene.collection.objects.link(result)
    result.name = 'Retopo_SM_NameContract'
    return source, result, {
      'asset_base': 'SM_NameContract',
      'source': {
        'stable_id': 'name-contract-source',
        'content_signature': mesh_object_content_signature(
          source,
          bpy.context.scene,
        ),
      },
      'qr': {'api_version': 1, 'preexisting_mesh_objects': [source.name]},
    }

  def test_adoption_rejects_texture_set_filename_collision_before_mutation(self):
    from substance_tools.meshy_pipeline import MeshyPipelineError, adopt_qr_result

    source, result, state = self._simple_adoption_pair(
      ('M_Fabric-Red', 'M_Fabric_Red')
    )
    original_names = [material.name for material in source.data.materials]
    with self.assertRaisesRegex(MeshyPipelineError, 'collide'):
      adopt_qr_result(bpy.context.scene, source, result, state)
    self.assertEqual(source.name, 'SM_NameContract')
    self.assertEqual(result.name, 'Retopo_SM_NameContract')
    self.assertEqual(
      [material.name for material in source.data.materials],
      original_names,
    )

  def test_adoption_persists_one_canonical_texture_set_id(self):
    from substance_tools.core import low_texture_set_names
    from substance_tools.meshy_pipeline import (
      MATERIAL_TEXTURE_SET_PROPERTY,
      adopt_qr_result,
    )

    source, result, state = self._simple_adoption_pair(('M_Fabric Red.v1',))
    receipt = adopt_qr_result(bpy.context.scene, source, result, state)
    self.assertEqual(low_texture_set_names([result]), ['Fabric_Red_v1'])
    self.assertEqual(result.data.materials[0].name, 'M_Fabric_Red_v1')
    self.assertEqual(
      result.data.materials[0][MATERIAL_TEXTURE_SET_PROPERTY],
      'Fabric_Red_v1',
    )
    self.assertEqual(receipt['materials'][0]['texture_set'], 'Fabric_Red_v1')

  def test_adoption_rejects_missing_polygon_used_texture_set_once(self):
    from substance_tools.meshy_pipeline import MeshyPipelineError, adopt_qr_result

    source, result, state = self._simple_adoption_pair(('M_SetA', 'M_SetB'))
    source.data.polygons[-1].material_index = 1
    self.assertTrue(all(polygon.material_index == 0 for polygon in result.data.polygons))
    with self.assertRaisesRegex(MeshyPipelineError, 'polygon-used Texture Sets'):
      adopt_qr_result(bpy.context.scene, source, result, state)
    self.assertEqual(source.name, 'SM_NameContract')
    self.assertEqual(result.name, 'Retopo_SM_NameContract')

  def test_late_adoption_failure_rolls_back_all_scene_mutation(self):
    import substance_tools.meshy_pipeline as pipeline

    source, result, state = self._simple_adoption_pair(('M_Rollback',))
    source_signature = pipeline.mesh_object_content_signature(
      source,
      bpy.context.scene,
    )
    result_signature = pipeline.mesh_object_content_signature(
      result,
      bpy.context.scene,
    )
    source_name = source.name
    result_name = result.name
    result_mesh_pointer = result.data.as_pointer()
    material_pointer = source.data.materials[0].as_pointer()
    source_collections = {collection.as_pointer() for collection in source.users_collection}
    result_collections = {collection.as_pointer() for collection in result.users_collection}
    original_sync = pipeline._sync_low_export_via_ue_unique

    def fail_late(*args, **kwargs):
      raise RuntimeError('intentional late receipt failure')

    pipeline._sync_low_export_via_ue_unique = fail_late
    try:
      with self.assertRaisesRegex(RuntimeError, 'intentional late'):
        pipeline.adopt_qr_result(bpy.context.scene, source, result, state)
    finally:
      pipeline._sync_low_export_via_ue_unique = original_sync

    self.assertEqual(source.name, source_name)
    self.assertEqual(result.name, result_name)
    self.assertEqual(result.data.as_pointer(), result_mesh_pointer)
    self.assertEqual(source.data.materials[0].as_pointer(), material_pointer)
    self.assertEqual(result.data.materials[0].as_pointer(), material_pointer)
    self.assertEqual(
      {collection.as_pointer() for collection in source.users_collection},
      source_collections,
    )
    self.assertEqual(
      {collection.as_pointer() for collection in result.users_collection},
      result_collections,
    )
    self.assertEqual(
      pipeline.mesh_object_content_signature(source, bpy.context.scene),
      source_signature,
    )
    self.assertEqual(
      pipeline.mesh_object_content_signature(result, bpy.context.scene),
      result_signature,
    )
    self.assertFalse(any(
      material.name.startswith('__SubstanceTools')
      for material in bpy.data.materials
    ))

  def test_two_texture_sets_archive_adopt_and_bake_distinct_maps(self):
    from substance_tools.meshy_pipeline import (
      SOURCE_ID_PROPERTY,
      STATE_SCHEMA_VERSION,
      UV_METHOD_VERSION,
      adopt_qr_result,
      advance_pipeline_state,
      analyze_mesh_object,
      create_source_archive,
      store_pipeline_state,
    )

    with tempfile.TemporaryDirectory(prefix='st_meshy_multi_smoke_') as raw:
      root = Path(raw)
      images_by_set = {}
      source_paths = []
      for texture_set in TEXTURE_SETS:
        images_by_set[texture_set] = {}
        for role in ('Color', 'Extra', 'Normal'):
          image, path = _write_external_constant_image(
            root,
            texture_set,
            role,
            SOURCE_VALUES[texture_set][role],
          )
          images_by_set[texture_set][role] = image
          source_paths.append(path)

      self.assertEqual({path.name for path in source_paths}, {SOURCE_BASENAME})
      self.assertEqual(len({path.parent for path in source_paths}), 6)

      bpy.ops.mesh.primitive_cube_add(size=2.0)
      source = bpy.context.object
      source.name = 'SM_MultiMaterialAsset'
      _assign_overlapping_valid_uv(source.data)
      for texture_set in TEXTURE_SETS:
        source.data.materials.append(
          _make_source_material(texture_set, images_by_set[texture_set])
        )
      for polygon in source.data.polygons:
        polygon.material_index = 0 if polygon.index < 3 else 1

      blend_path = root / 'MultiMaterialAsset.blend'
      bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False)
      self.assertFalse(bpy.data.is_dirty)

      analysis = analyze_mesh_object(source, bpy.context.scene)
      self.assertFalse(bpy.data.is_dirty, 'Analyze must remain read-only')
      discovered = {
        (entry['texture_set'], entry['role'], Path(entry['file_path']).name)
        for entry in analysis['source_images']
        if entry['role'] in {'Color', 'Extra', 'Normal'}
      }
      self.assertEqual(
        discovered,
        {
          (texture_set, role, SOURCE_BASENAME)
          for texture_set in TEXTURE_SETS
          for role in ('Color', 'Extra', 'Normal')
        },
      )

      archive = create_source_archive(analysis['base_name'], analysis)
      logical_paths = [entry['logical_path'] for entry in archive['entries']]
      self.assertEqual(len(logical_paths), 7)
      self.assertEqual(
        len({path.casefold() for path in logical_paths}),
        len(logical_paths),
      )
      self.assertEqual(
        {
          path for path in logical_paths if path.startswith('texture/')
        },
        {
          f'texture/{texture_set}/{role}/{SOURCE_BASENAME}'
          for texture_set in TEXTURE_SETS
          for role in ('Color', 'Extra', 'Normal')
        },
      )
      for entry in archive['entries']:
        self.assertEqual(entry['source']['sha256'], entry['backup']['sha256'])

      stable_id = 'multi-material-smoke-source'
      source[SOURCE_ID_PROPERTY] = stable_id
      state = {
        'schema_version': STATE_SCHEMA_VERSION,
        'stage': 'ANALYZED',
        'asset_base': analysis['base_name'],
        'analysis': analysis,
        'source': {
          'object_name': source.name,
          'mesh_name': source.data.name,
          'stable_id': stable_id,
          'content_signature': analysis['content_signature'],
        },
        'archive': {'source_original': archive},
        'checkpoints': {'ANALYZED': analysis},
      }
      state = advance_pipeline_state(
        state,
        'SOURCE_ARCHIVED',
        {'archive_root': archive['root'], 'entry_count': len(archive['entries'])},
      )
      state['qr'] = {
        'api_version': 1,
        'operator_invoked': False,
        'preexisting_mesh_objects': [source.name],
      }
      state = advance_pipeline_state(state, 'QR_READY', state['qr'])

      result = source.copy()
      result.data = source.data.copy()
      bpy.context.scene.collection.objects.link(result)
      result.name = 'Retopo_SM_MultiMaterialAsset'
      receipt = adopt_qr_result(
        bpy.context.scene,
        source,
        result,
        state,
        resolution=512,
        margin_pixels=4,
      )
      self.assertEqual(receipt['high_object'], 'SM_MultiMaterialAsset_high')
      self.assertEqual(receipt['low_object'], 'SM_MultiMaterialAsset_low')
      self.assertEqual(len(receipt['materials']), 2)
      self.assertNotIn('uv', receipt)

      state['low'] = receipt
      state = advance_pipeline_state(state, 'LOW_CREATED', receipt)
      uv_receipt = {
        'valid': True,
        'layer': result.data.uv_layers.active.name,
        'loop_count': len(result.data.loops),
        'outside_loops': 0,
        'nonfinite_loops': 0,
        'zero_area_faces': 0,
        'created': True,
        'method': UV_METHOD_VERSION,
        'resolution': 512,
        'margin_pixels': 4,
      }
      self.assertTrue(uv_receipt['valid'])
      uv_request = {
        'method': UV_METHOD_VERSION,
        'status': 'RUNNING',
        'low_object': result.name,
        'low_object_pointer': int(result.as_pointer()),
        'polygon_count': len(result.data.polygons),
      }
      state['low']['uvgami'] = uv_request
      state = advance_pipeline_state(state, 'UVGAMI_RUNNING', uv_request)
      state['low']['uv'] = uv_receipt
      state = advance_pipeline_state(state, 'UV_READY', uv_receipt)
      store_pipeline_state(bpy.context.scene, state)

      bpy.context.scene.substance_tools_baking.resolution = '512'
      bake_result = bpy.ops.st.bake_meshy_source_maps()
      self.assertEqual(bake_result, {'FINISHED'})
      baked = json.loads(
        bpy.context.scene['_substance_tools_meshy_source_maps_receipt']
      )
      self.assertEqual(baked['texture_sets'], list(TEXTURE_SETS))
      self.assertEqual(baked['painter_texture_sets'], list(TEXTURE_SETS))
      self.assertEqual(baked['normal_convention'], 'DIRECTX')
      self.assertEqual(baked['snapshot_files'], 15)
      self.assertEqual(
        Path(baked['snapshot_dir']).parent,
        Path(archive['root']).parent,
      )

      expected_roles = {
        'BaseColor',
        'Extra',
        'ExtraR',
        'Roughness',
        'Metallic',
        'Normal',
      }
      pixel_receipts = {}
      for texture_set in TEXTURE_SETS:
        self.assertEqual(set(baked['maps'][texture_set]), expected_roles)
        pixel_receipts[texture_set] = {}
        for role, path_value in baked['maps'][texture_set].items():
          path = Path(path_value)
          self.assertTrue(path.is_file(), path)
          pixel_receipts[texture_set][role] = _pixel_receipt(path)

      for role in sorted(expected_roles):
        self.assertNotEqual(
          pixel_receipts['MultiSetA'][role]['digest'],
          pixel_receipts['MultiSetB'][role]['digest'],
          f'{role} output pixels must distinguish the two Texture Sets',
        )

      for role, source_role in (
        ('ExtraR', 'Extra'),
        ('Roughness', 'Extra'),
        ('Metallic', 'Extra'),
      ):
        channel_index = {'ExtraR': 0, 'Roughness': 1, 'Metallic': 2}[role]
        for texture_set in TEXTURE_SETS:
          expected = SOURCE_VALUES[texture_set][source_role][channel_index]
          observed = pixel_receipts[texture_set][role]['maximum']
          self.assertAlmostEqual(observed, expected, delta=0.04, msg=(texture_set, role))


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

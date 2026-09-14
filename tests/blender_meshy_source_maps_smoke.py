"""Run with Blender --factory-startup; never writes user preferences."""

from array import array
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import addon_utils
import bpy


def _constant_image(name, rgba, colorspace, role):
  image = bpy.data.images.new(name, width=8, height=8, alpha=False)
  image.colorspace_settings.name = colorspace
  image.pixels.foreach_set(array('f', rgba) * 64)
  image['_ue_unique_export_original_name'] = role
  image.update()
  return image


class MeshySourceMapBakeSmokeTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    addon_utils.enable('substance_tools', default_set=False, persistent=False)

  @classmethod
  def tearDownClass(cls):
    addon_utils.disable('substance_tools', default_set=False)

  def test_all_source_roles_bake_archive_and_reuse(self):
    from substance_tools.core import ensure_baking_collections
    from substance_tools.meshy_pipeline import (
      STATE_PROPERTY,
      STATE_SCHEMA_VERSION,
      load_pipeline_state,
      mesh_object_content_signature,
      store_pipeline_state,
    )
    from substance_tools.meshy_pipeline_contract import publish_immutable_snapshot_set

    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    _root, low_collection, high_collection, _alpha = ensure_baking_collections(
      bpy.context.scene
    )

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    high = bpy.context.object
    high.name = 'SM_TestAsset_high'
    low = high.copy()
    low.data = high.data.copy()
    bpy.context.scene.collection.objects.link(low)
    low.name = 'SM_TestAsset_low'

    high_material = bpy.data.materials.new('__SubstanceToolsHigh_M_TestAsset')
    high_material.use_nodes = True
    nodes = high_material.node_tree.nodes
    links = high_material.node_tree.links
    principled = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')

    color = _constant_image(
      'T_TestAsset_Color', (0.20, 0.40, 0.60, 1.0), 'sRGB', 'base_color'
    )
    extra = _constant_image(
      'T_TestAsset_Extra', (0.25, 0.65, 0.85, 1.0), 'Non-Color',
      'metallic_roughness',
    )
    normal = _constant_image(
      'T_TestAsset_Normal', (0.50, 0.50, 1.0, 1.0), 'Non-Color', 'normal'
    )
    color_node = nodes.new('ShaderNodeTexImage')
    color_node.image = color
    links.new(color_node.outputs['Color'], principled.inputs['Base Color'])
    extra_node = nodes.new('ShaderNodeTexImage')
    extra_node.image = extra
    separate = nodes.new('ShaderNodeSeparateColor')
    links.new(extra_node.outputs['Color'], separate.inputs['Color'])
    links.new(separate.outputs['Green'], principled.inputs['Roughness'])
    links.new(separate.outputs['Blue'], principled.inputs['Metallic'])
    normal_node = nodes.new('ShaderNodeTexImage')
    normal_node.image = normal
    normal_map = nodes.new('ShaderNodeNormalMap')
    links.new(normal_node.outputs['Color'], normal_map.inputs['Color'])
    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
    high.data.materials.append(high_material)

    low_material = bpy.data.materials.new('M_TestAsset')
    low_material.use_nodes = True
    low.data.materials.clear()
    low.data.materials.append(low_material)

    for obj, collection in ((high, high_collection), (low, low_collection)):
      for current in list(obj.users_collection):
        current.objects.unlink(obj)
      collection.objects.link(obj)

    high_material_pointer = high.material_slots[0].material.as_pointer()
    high_node_count = len(high.material_slots[0].material.node_tree.nodes)
    low_material_pointer = low.material_slots[0].material.as_pointer()
    low_node_count = len(low.material_slots[0].material.node_tree.nodes)

    with tempfile.TemporaryDirectory(prefix='st_meshy_bake_smoke_') as raw:
      blend_path = Path(raw) / 'TestAsset.blend'
      bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False)
      source_archive_root = (
        Path(raw) / '_painter_archive' / 'SM_TestAsset' / '00_source_original_once'
      )
      source_archive_root.parent.mkdir(parents=True, exist_ok=True)
      source_manifest = publish_immutable_snapshot_set(
        {'scene/TestAsset.blend': blend_path},
        source_archive_root,
      )
      source_manifest_path = source_archive_root / 'manifest.json'
      store_pipeline_state(bpy.context.scene, {
        'schema_version': STATE_SCHEMA_VERSION,
        'stage': 'UV_READY',
        'asset_base': 'SM_TestAsset',
        'analysis': {'target_quads': 5000},
        'source': {
          'object_name': high.name,
          'stable_id': 'smoke-source',
          'content_signature': mesh_object_content_signature(
            high,
            bpy.context.scene,
          ),
        },
        'low': {
          'high_object': high.name,
          'low_object': low.name,
          'content_signatures': {
            'high': mesh_object_content_signature(high, bpy.context.scene),
            'low': mesh_object_content_signature(low, bpy.context.scene),
          },
        },
        'archive': {
          'source_original': {
            'root': str(source_archive_root),
            'manifest_path': str(source_manifest_path),
            'manifest_sha256': hashlib.sha256(
              source_manifest_path.read_bytes()
            ).hexdigest(),
            'entries': [{
              'logical_path': 'scene/TestAsset.blend',
              'role': 'Blend',
              'source_path': str(blend_path),
              'source': source_manifest['files'][0]['source'],
              'backup': source_manifest['files'][0]['backup'],
            }],
          },
        },
        'checkpoints': {},
      })

      bpy.context.scene.substance_tools_baking.resolution = '512'
      first_result = bpy.ops.st.bake_meshy_source_maps()
      self.assertEqual(first_result, {'FINISHED'})
      first = json.loads(
        bpy.context.scene['_substance_tools_meshy_source_maps_receipt']
      )
      self.assertEqual(first['texture_sets'], ['TestAsset'])
      self.assertEqual(first['painter_texture_sets'], ['TestAsset'])
      self.assertEqual(first['normal_convention'], 'DIRECTX')
      self.assertEqual(
        set(first['maps']['TestAsset']),
        {'BaseColor', 'Extra', 'ExtraR', 'Roughness', 'Metallic', 'Normal'},
      )
      self.assertEqual(first['snapshot_files'], 9)
      self.assertTrue(Path(first['snapshot_manifest']).is_file())
      for path in first['maps']['TestAsset'].values():
        self.assertTrue(Path(path).is_file(), path)
      self.assertTrue(Path(first['fbx']['low']).is_file())
      self.assertTrue(Path(first['fbx']['high']).is_file())

      for role, expected in (
        ('ExtraR', 0.25),
        ('Roughness', 0.65),
        ('Metallic', 0.85),
      ):
        baked = bpy.data.images.load(
          first['maps']['TestAsset'][role],
          check_existing=False,
        )
        pixels = array('f', [0.0]) * len(baked.pixels)
        baked.pixels.foreach_get(pixels)
        observed = max(pixels[0::4])
        self.assertAlmostEqual(observed, expected, delta=0.03, msg=role)
        bpy.data.images.remove(baked)

      archive_root = Path(first['snapshot_dir'])
      self.assertEqual(archive_root.parent, source_archive_root.parent)
      before = {
        path.relative_to(archive_root).as_posix(): path.stat().st_mtime_ns
        for path in archive_root.rglob('*') if path.is_file()
      }
      second_result = bpy.ops.st.bake_meshy_source_maps()
      self.assertEqual(second_result, {'FINISHED'})
      after = {
        path.relative_to(archive_root).as_posix(): path.stat().st_mtime_ns
        for path in archive_root.rglob('*') if path.is_file()
      }
      self.assertEqual(after, before)
      self.assertEqual(load_pipeline_state(bpy.context.scene)['stage'], 'BAKE_BASELINE_ARCHIVED')
      self.assertEqual(
        load_pipeline_state(bpy.context.scene)['painter_package']['resolution'],
        512,
      )

      working_color = Path(first['maps']['TestAsset']['BaseColor'])
      archived_color = archive_root / 'texture' / working_color.name
      expected_hash = hashlib.sha256(archived_color.read_bytes()).hexdigest()
      working_color.write_bytes(b'corrupt-working-copy')
      self.assertEqual(bpy.ops.st.bake_meshy_source_maps(), {'FINISHED'})
      self.assertEqual(
        hashlib.sha256(working_color.read_bytes()).hexdigest(),
        expected_hash,
      )

      state_before_resolution_error = bpy.context.scene[STATE_PROPERTY]
      bpy.context.scene.substance_tools_baking.resolution = '1024'
      with self.assertRaisesRegex(RuntimeError, 'baseline is 512px'):
        bpy.ops.st.bake_meshy_source_maps()
      self.assertEqual(
        bpy.context.scene[STATE_PROPERTY],
        state_before_resolution_error,
      )

    self.assertEqual(high.material_slots[0].material.as_pointer(), high_material_pointer)
    self.assertEqual(len(high.material_slots[0].material.node_tree.nodes), high_node_count)
    self.assertEqual(low.material_slots[0].material.as_pointer(), low_material_pointer)
    self.assertEqual(len(low.material_slots[0].material.node_tree.nodes), low_node_count)


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

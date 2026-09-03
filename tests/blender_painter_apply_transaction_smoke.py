"""Factory-startup smoke tests for transactional Painter texture apply.

Run only inside Blender.  The suite uses temporary texture directories, enables
the add-on with ``default_set=False``, and never saves Blender preferences.
"""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import addon_utils
import bpy


def _hash(path):
  return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_png(path, rgba):
  image = bpy.data.images.new(
    f'__transaction_fixture_{Path(path).stem}',
    width=2,
    height=2,
    alpha=True,
  )
  image.pixels = list(rgba) * 4
  image.filepath_raw = str(path)
  image.file_format = 'PNG'
  image.save()
  bpy.data.images.remove(image)


class PainterApplyTransactionSmokeTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    addon_utils.enable('substance_tools', default_set=False, persistent=False)
    from substance_tools import core
    cls.core = core

  @classmethod
  def tearDownClass(cls):
    addon_utils.disable('substance_tools', default_set=False)

  def setUp(self):
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for material in list(bpy.data.materials):
      bpy.data.materials.remove(material)
    for image in list(bpy.data.images):
      if image.name.startswith(('T_QA_', 'T_Different_', '__transaction_fixture_')):
        bpy.data.images.remove(image)
    self.temp = tempfile.TemporaryDirectory(prefix='st_apply_transaction_smoke_')
    self.base_dir = Path(self.temp.name)
    self.texture_dir = self.base_dir / 'texture'
    self.texture_dir.mkdir()

  def tearDown(self):
    for image in list(bpy.data.images):
      source = bpy.path.abspath(image.filepath_raw or image.filepath)
      if source and str(source).startswith(str(self.texture_dir)):
        bpy.data.images.remove(image)
    self.temp.cleanup()

  def _low_object(self, material_name='M_QA'):
    mesh = bpy.data.meshes.new(f'{material_name}_mesh')
    mesh.from_pydata(
      [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
      [],
      [(0, 1, 2)],
    )
    low = bpy.data.objects.new(f'{material_name}_low', mesh)
    bpy.context.scene.collection.objects.link(low)
    material = bpy.data.materials.new(material_name)
    material.use_nodes = True
    mesh.materials.append(material)
    return low, material

  def _texture_fixture(self, roles=('Color', 'Extra', 'Normal'), texture_set='QA'):
    old_values = {
      'Color': (0.95, 0.02, 0.02, 1.0),
      'Extra': (0.02, 0.95, 0.02, 1.0),
      'Normal': (0.02, 0.02, 0.95, 1.0),
      'Emissive': (0.95, 0.50, 0.02, 1.0),
      'Height': (0.40, 0.40, 0.40, 1.0),
    }
    new_values = {
      'Color': (0.20, 0.40, 0.60, 1.0),
      'Extra': (0.10, 0.30, 0.80, 1.0),
      'Normal': (0.50, 0.25, 1.00, 1.0),
      'Emissive': (0.10, 0.60, 0.20, 1.0),
      'Height': (0.70, 0.70, 0.70, 1.0),
    }
    canonical = {}
    incoming = {}
    for role in roles:
      canonical[role] = self.texture_dir / f'T_{texture_set}_{role}.png'
      incoming[role] = self.texture_dir / f'M_{texture_set}_{role}.png'
      _write_png(canonical[role], old_values[role])
      _write_png(incoming[role], new_values[role])
    return {
      'result': {
        'textures': {texture_set: [str(incoming[role]) for role in roles]},
      },
      'canonical': canonical,
      'incoming': incoming,
      'old_hashes': {role: _hash(path) for role, path in canonical.items()},
      'new_hashes': {role: _hash(path) for role, path in incoming.items()},
    }

  def _assert_no_transaction_residue(self):
    self.assertEqual(
      list(self.texture_dir.glob('.substance_tools_apply_*')),
      [],
    )

  def _assert_rolled_back(self, fixture):
    self.assertEqual(
      {role: _hash(path) for role, path in fixture['canonical'].items()},
      fixture['old_hashes'],
    )
    self.assertTrue(all(path.is_file() for path in fixture['incoming'].values()))
    self.assertEqual(
      {role: _hash(path) for role, path in fixture['incoming'].items()},
      fixture['new_hashes'],
    )
    self._assert_no_transaction_residue()

  def test_success_commits_only_after_material_role_verification(self):
    low, material = self._low_object()
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')
    artist_emission = nodes.new('ShaderNodeRGB')
    artist_emission.name = 'Artist Emission'
    links.new(artist_emission.outputs['Color'], principled.inputs['Emission Color'])
    artist_custom = nodes.new('ShaderNodeValue')
    artist_custom.name = 'Artist Unlinked Custom'
    preexisting_normal = nodes.new('ShaderNodeNormalMap')
    preexisting_normal.name = 'Painter Normal'
    preexisting_normal.space = 'OBJECT'
    preexisting_normal_channels = nodes.new('ShaderNodeSeparateColor')
    preexisting_normal_channels.name = 'Painter Normal DirectX Channels'
    preexisting_normal_channels.mode = 'HSV'
    preexisting_normal_combine = nodes.new('ShaderNodeCombineColor')
    preexisting_normal_combine.name = 'Painter Normal OpenGL'
    preexisting_normal_combine.mode = 'HSL'
    preexisting_extra_channels = nodes.new('ShaderNodeSeparateColor')
    preexisting_extra_channels.name = 'Painter Extra Channels'
    preexisting_extra_channels.mode = 'HSV'
    bypass_normal = nodes.new('ShaderNodeRGB')
    bypass_normal.name = 'Artist Normal Bypass'
    links.new(bypass_normal.outputs['Color'], principled.inputs['Normal'])
    render_attribute = None
    if hasattr(material, 'surface_render_method'):
      material.surface_render_method = 'DITHERED'
      render_attribute = ('surface_render_method', material.surface_render_method)
    elif hasattr(material, 'blend_method'):
      material.blend_method = 'BLEND'
      render_attribute = ('blend_method', material.blend_method)
    fixture = self._texture_fixture()

    receipt = self.core.apply_painter_export_transaction(
      fixture['result'],
      [low],
      self.texture_dir,
      meshy_mode=True,
    )

    self.assertEqual(receipt['applied'], 1)
    self.assertEqual(
      receipt['managed_roles'],
      {'M_QA': ['Color', 'Extra', 'Normal']},
    )
    self.assertEqual(
      {role: _hash(path) for role, path in fixture['canonical'].items()},
      fixture['new_hashes'],
    )
    self.assertTrue(all(not path.exists() for path in fixture['incoming'].values()))
    self._assert_no_transaction_residue()

    final_material = low.material_slots[0].material
    self.assertIsNot(final_material, material)
    nodes = final_material.node_tree.nodes
    principled = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')
    green_flip = nodes.get('Painter Normal DirectX Green Flip')
    self.assertIsNotNone(green_flip)
    self.assertEqual(green_flip.operation, 'SUBTRACT')
    self.assertAlmostEqual(green_flip.inputs[0].default_value, 1.0)
    normal_channels = nodes.get('Painter Normal DirectX Channels')
    normal_combine = nodes.get('Painter Normal OpenGL')
    normal_map = nodes.get('Painter Normal')
    normal_texture = nodes.get('Painter Normal Texture')
    self.assertEqual(normal_map.space, 'TANGENT')
    self.assertEqual(normal_channels.mode, 'RGB')
    self.assertEqual(normal_combine.mode, 'RGB')
    self.assertEqual(normal_texture.image.colorspace_settings.name, 'Non-Color')
    self.assertTrue(any(
      link.from_node == normal_texture
      for link in normal_channels.inputs['Color'].links
    ))
    self.assertTrue(any(
      link.from_node == normal_channels and link.from_socket.name == 'Green'
      for link in green_flip.inputs[1].links
    ))
    self.assertTrue(any(
      link.from_node == green_flip
      for link in normal_combine.inputs['Green'].links
    ))
    self.assertTrue(any(
      link.from_node == normal_combine
      for link in normal_map.inputs['Color'].links
    ))
    self.assertTrue(any(
      link.from_node == normal_map
      for link in principled.inputs['Normal'].links
    ))
    self.assertEqual(
      nodes.get('Painter Color').image.colorspace_settings.name,
      'sRGB',
    )
    extra = nodes.get('Painter Extra Channels')
    self.assertEqual(extra.mode, 'RGB')
    self.assertEqual(
      nodes.get('Painter Extra').image.colorspace_settings.name,
      'Non-Color',
    )
    self.assertIsNotNone(nodes.get('Artist Unlinked Custom'))
    self.assertIsNotNone(nodes.get('Artist Normal Bypass'))
    self.assertTrue(any(
      link.from_node.name == 'Artist Emission'
      for link in principled.inputs['Emission Color'].links
    ))
    if render_attribute:
      self.assertEqual(
        getattr(final_material, render_attribute[0]),
        render_attribute[1],
      )
    self.assertTrue(any(
      link.from_node == extra and link.from_socket.name == 'Green'
      for link in principled.inputs['Roughness'].links
    ))
    self.assertTrue(any(
      link.from_node == extra and link.from_socket.name == 'Blue'
      for link in principled.inputs['Metallic'].links
    ))
    self.assertEqual(
      self.core.verify_painter_material_roles(
        [],
        self.texture_dir,
        {'Color', 'Extra', 'Normal'},
        material_texture_sets={final_material: 'QA'},
      ),
      {'M_QA': ['Color', 'Extra', 'Normal']},
    )
    final_material.node_tree.links.new(
      nodes.get('Artist Normal Bypass').outputs['Color'],
      principled.inputs['Normal'],
    )
    with self.assertRaisesRegex(RuntimeError, 'Normal is not connected'):
      self.core.verify_painter_material_roles(
        [],
        self.texture_dir,
        {'Color', 'Extra', 'Normal'},
        material_texture_sets={final_material: 'QA'},
      )

  def test_source_backed_role_contract_allows_color_only(self):
    low, original_material = self._low_object()
    fixture = self._texture_fixture(('Color',))

    receipt = self.core.apply_painter_export_transaction(
      fixture['result'],
      [low],
      self.texture_dir,
      meshy_mode=True,
      canonical_texture_sets={'QA'},
      required_roles_by_texture_set={'QA': ['Color']},
    )

    self.assertEqual(receipt['managed_roles'], {'M_QA': ['Color']})
    self.assertEqual(_hash(fixture['canonical']['Color']), fixture['new_hashes']['Color'])
    self.assertFalse(fixture['incoming']['Color'].exists())
    final_material = low.material_slots[0].material
    self.assertIsNot(final_material, original_material)
    nodes = final_material.node_tree.nodes
    self.assertIsNotNone(nodes.get('Painter Color'))
    self.assertIsNone(nodes.get('Painter Extra'))
    self.assertIsNone(nodes.get('Painter Normal Texture'))
    self.assertEqual(
      self.core.verify_painter_material_roles(
        [low],
        self.texture_dir,
        {'QA': ['Color']},
      ),
      {'M_QA': ['Color']},
    )
    self._assert_no_transaction_residue()

  def test_source_backed_roles_can_differ_between_texture_sets(self):
    low_a, _material_a = self._low_object('M_QA_A')
    low_b, _material_b = self._low_object('M_QA_B')
    fixture_a = self._texture_fixture(('Color',), texture_set='QA_A')
    fixture_b = self._texture_fixture(
      ('Color', 'Normal'),
      texture_set='QA_B',
    )
    result = {
      'textures': {
        **fixture_a['result']['textures'],
        **fixture_b['result']['textures'],
      },
    }

    receipt = self.core.apply_painter_export_transaction(
      result,
      [low_a, low_b],
      self.texture_dir,
      meshy_mode=True,
      canonical_texture_sets={'QA_A', 'QA_B'},
      required_roles_by_texture_set={
        'QA_A': ['Color'],
        'QA_B': ['Color', 'Normal'],
      },
    )

    self.assertEqual(receipt['managed_roles'], {
      'M_QA_A': ['Color'],
      'M_QA_B': ['Color', 'Normal'],
    })
    self.assertIsNone(
      low_a.material_slots[0].material.node_tree.nodes.get('Painter Normal Texture')
    )
    self.assertIsNotNone(
      low_b.material_slots[0].material.node_tree.nodes.get('Painter Normal Texture')
    )
    self._assert_no_transaction_residue()

  def test_second_material_failure_restores_slots_graphs_and_full_file_group(self):
    low_a, material_a = self._low_object('M_QA_A')
    low_b, material_b = self._low_object('M_QA_B')
    for material in (material_a, material_b):
      nodes = material.node_tree.nodes
      principled = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')
      custom = nodes.new('ShaderNodeValue')
      custom.name = f'Artist Custom {material.name}'
      emission = nodes.new('ShaderNodeRGB')
      emission.name = f'Artist Emission {material.name}'
      material.node_tree.links.new(
        emission.outputs['Color'],
        principled.inputs['Emission Color'],
      )
    pointers = {
      low_a.name: low_a.material_slots[0].material.as_pointer(),
      low_b.name: low_b.material_slots[0].material.as_pointer(),
    }
    graph_signatures = {
      material.name: (
        sorted(node.name for node in material.node_tree.nodes),
        material.node_tree.nodes.get(f'Artist Emission {material.name}').as_pointer(),
      )
      for material in (material_a, material_b)
    }
    fixture_a = self._texture_fixture(texture_set='QA_A')
    fixture_b = self._texture_fixture(texture_set='QA_B')
    original_images = []
    for fixture in (fixture_a, fixture_b):
      for role, path in fixture['canonical'].items():
        image = bpy.data.images.load(str(path), check_existing=False)
        image.name = f'Artist Original Runtime {path.stem}'
        original_images.append({
          'image': image,
          'pointer': image.as_pointer(),
          'pixels': tuple(image.pixels[:4]),
          'role': role,
        })
    result = {
      'textures': {
        **fixture_a['result']['textures'],
        **fixture_b['result']['textures'],
      },
    }
    real_apply = self.core.apply_meshy_painter_textures_to_material
    calls = {'count': 0}

    def fail_second(*args, **kwargs):
      calls['count'] += 1
      if calls['count'] == 2:
        raise RuntimeError('injected second material failure')
      return real_apply(*args, **kwargs)

    with mock.patch.object(
      self.core,
      'apply_meshy_painter_textures_to_material',
      side_effect=fail_second,
    ):
      with self.assertRaisesRegex(RuntimeError, 'injected second material failure'):
        self.core.apply_painter_export_transaction(
          result,
          [low_a, low_b],
          self.texture_dir,
          meshy_mode=True,
        )

    self.assertEqual(low_a.material_slots[0].material.as_pointer(), pointers[low_a.name])
    self.assertEqual(low_b.material_slots[0].material.as_pointer(), pointers[low_b.name])
    for material in (material_a, material_b):
      expected_nodes, emission_pointer = graph_signatures[material.name]
      self.assertEqual(
        sorted(node.name for node in material.node_tree.nodes),
        expected_nodes,
      )
      self.assertEqual(
        material.node_tree.nodes.get(
          f'Artist Emission {material.name}'
        ).as_pointer(),
        emission_pointer,
      )
      principled = next(
        node for node in material.node_tree.nodes
        if node.type == 'BSDF_PRINCIPLED'
      )
      self.assertTrue(any(
        link.from_node.name == f'Artist Emission {material.name}'
        for link in principled.inputs['Emission Color'].links
      ))
    self._assert_rolled_back(fixture_a)
    self._assert_rolled_back(fixture_b)
    for record in original_images:
      image = record['image']
      self.assertEqual(image.as_pointer(), record['pointer'])
      self.assertEqual(tuple(image.pixels[:4]), record['pixels'])
    self.assertFalse(any(
      image.name.startswith('__ST_PainterApply_')
      for image in bpy.data.images
    ))

  def test_zero_apply_rolls_back_full_group_and_preserves_staging(self):
    low, _material = self._low_object('M_Different')
    fixture = self._texture_fixture()

    with self.assertRaises(self.core.PainterApplyNoMaterialsError):
      self.core.apply_painter_export_transaction(
        fixture['result'],
        [low],
        self.texture_dir,
        meshy_mode=False,
      )

    self._assert_rolled_back(fixture)

  def test_before_commit_checkpoint_failure_rolls_back_files_and_material(self):
    low, original_material = self._low_object()
    original_pointer = original_material.as_pointer()
    original_nodes = sorted(
      node.name for node in original_material.node_tree.nodes
    )
    fixture = self._texture_fixture()
    callback_observation = {}

    def fail_checkpoint(receipt):
      callback_observation['roles'] = receipt['managed_roles']
      callback_observation['installed'] = {
        role: _hash(path)
        for role, path in fixture['canonical'].items()
      }
      raise RuntimeError('injected CANONICAL_APPLIED store failure')

    with self.assertRaisesRegex(RuntimeError, 'store failure'):
      self.core.apply_painter_export_transaction(
        fixture['result'],
        [low],
        self.texture_dir,
        meshy_mode=True,
        before_commit=fail_checkpoint,
      )

    self.assertEqual(
      callback_observation['roles'],
      {'M_QA': ['Color', 'Extra', 'Normal']},
    )
    self.assertEqual(callback_observation['installed'], fixture['new_hashes'])
    self.assertEqual(low.material_slots[0].material.as_pointer(), original_pointer)
    self.assertEqual(
      sorted(node.name for node in original_material.node_tree.nodes),
      original_nodes,
    )
    self._assert_rolled_back(fixture)

  def test_meshy_mode_filters_extra_preset_roles_and_preserves_their_staging(self):
    low, _material = self._low_object()
    fixture = self._texture_fixture(
      ('Color', 'Extra', 'Normal', 'Emissive', 'Height')
    )

    receipt = self.core.apply_painter_export_transaction(
      fixture['result'],
      [low],
      self.texture_dir,
      meshy_mode=True,
    )

    self.assertEqual(receipt['applied'], 1)
    for role in ('Color', 'Extra', 'Normal'):
      self.assertEqual(_hash(fixture['canonical'][role]), fixture['new_hashes'][role])
      self.assertFalse(fixture['incoming'][role].exists())
    for role in ('Emissive', 'Height'):
      self.assertEqual(_hash(fixture['canonical'][role]), fixture['old_hashes'][role])
      self.assertTrue(fixture['incoming'][role].is_file())
      self.assertEqual(_hash(fixture['incoming'][role]), fixture['new_hashes'][role])
    self._assert_no_transaction_residue()

  def test_meshy_mode_rejects_source_outside_texture_dir(self):
    low, _material = self._low_object()
    fixture = self._texture_fixture()
    with tempfile.TemporaryDirectory(prefix='st_apply_outside_') as outside_raw:
      outside = Path(outside_raw) / 'M_QA_Color.png'
      _write_png(outside, (0.8, 0.1, 0.3, 1.0))
      fixture['result']['textures']['QA'][0] = str(outside)

      with self.assertRaisesRegex(RuntimeError, 'directly inside texture_dir'):
        self.core.apply_painter_export_transaction(
          fixture['result'],
          [low],
          self.texture_dir,
          meshy_mode=True,
        )

    self._assert_rolled_back(fixture)

  def test_painter_matcher_name_collision_is_rejected_before_mutation(self):
    low_a, _material_a = self._low_object('M_Fabric-Red')
    low_b, _material_b = self._low_object('M_Fabric_Red')
    fixture_a = self._texture_fixture(texture_set='Fabric-Red')
    fixture_b = self._texture_fixture(texture_set='Fabric_Red')
    result = {
      'textures': {
        **fixture_a['result']['textures'],
        **fixture_b['result']['textures'],
      },
    }

    with self.assertRaisesRegex(RuntimeError, 'Painter name collisions'):
      self.core.apply_painter_export_transaction(
        result,
        [low_a, low_b],
        self.texture_dir,
        meshy_mode=True,
        canonical_texture_sets={'Fabric-Red', 'Fabric_Red'},
      )

    self._assert_rolled_back(fixture_a)
    self._assert_rolled_back(fixture_b)

  def test_legacy_canonicalize_remove_sources_false_stays_compatible(self):
    fixture = self._texture_fixture()
    installed = self.core.canonicalize_painter_export_files(
      fixture['result'],
      remove_sources=False,
    )
    self.assertEqual(
      set(installed),
      {str(path.resolve()) for path in fixture['canonical'].values()},
    )
    self.assertEqual(
      {role: _hash(path) for role, path in fixture['canonical'].items()},
      fixture['new_hashes'],
    )
    self.assertTrue(all(path.is_file() for path in fixture['incoming'].values()))
    self._assert_no_transaction_residue()


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

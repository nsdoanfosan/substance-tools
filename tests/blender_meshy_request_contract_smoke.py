"""Factory-startup tests for state-pinned Meshy Painter request data."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import addon_utils
import bpy


def _write_png(path, rgba):
  image = bpy.data.images.new(
    f'__meshy_contract_fixture_{Path(path).stem}',
    width=2,
    height=2,
    alpha=True,
  )
  image.pixels = list(rgba) * 4
  image.filepath_raw = str(path)
  image.file_format = 'PNG'
  image.save()
  bpy.data.images.remove(image)


class MeshyPainterRequestContractSmokeTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    addon_utils.enable('substance_tools', default_set=False, persistent=False)
    from substance_tools import core, operators
    cls.core = core
    cls.operators = operators

  @classmethod
  def tearDownClass(cls):
    addon_utils.disable('substance_tools', default_set=False)

  def setUp(self):
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for material in list(bpy.data.materials):
      bpy.data.materials.remove(material)
    for image in list(bpy.data.images):
      if image.name.startswith(('T_QA_', '__meshy_contract_fixture_')):
        bpy.data.images.remove(image)
    self.temp = tempfile.TemporaryDirectory(prefix='st_meshy_request_contract_')
    self.base = Path(self.temp.name)
    self.texture_dir = self.base / 'texture'
    self.low_dir = self.base / 'low'
    self.high_dir = self.base / 'high'
    for directory in (self.texture_dir, self.low_dir, self.high_dir):
      directory.mkdir()
    bpy.context.scene.substance_tools_baking.resolution = '512'

  def tearDown(self):
    for image in list(bpy.data.images):
      source = bpy.path.abspath(image.filepath_raw or image.filepath)
      if source and str(source).startswith(str(self.texture_dir)):
        bpy.data.images.remove(image)
    self.temp.cleanup()

  def _low_object(self):
    _root, low_collection, _high, _alpha = self.core.ensure_baking_collections(
      bpy.context.scene
    )
    mesh = bpy.data.meshes.new('QA_low_mesh')
    mesh.from_pydata(
      [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
      [],
      [(0, 1, 2)],
    )
    low = bpy.data.objects.new('QA_low', mesh)
    low_collection.objects.link(low)
    material = bpy.data.materials.new('M_QA')
    material.use_nodes = True
    mesh.materials.append(material)
    return low

  def _identity(self, path):
    return {
      'size': path.stat().st_size,
      'sha256': self.core.file_hash(path),
    }

  def _state_fixture(self, include_normal=True, material_roles=None):
    low_fbx = self.low_dir / 'SM_QA_low.fbx'
    high_fbx = self.high_dir / 'SM_QA_high.fbx'
    low_fbx.write_bytes(b'archived-low-fbx')
    high_fbx.write_bytes(b'archived-high-fbx')

    roles = list(
      material_roles
      if material_roles is not None
      else ['BaseColor', 'Extra', 'ExtraR', 'Roughness', 'Metallic']
    )
    if include_normal:
      roles.append('Normal')
    maps = {}
    for role in roles:
      path = self.texture_dir / f'{self.core.source_map_bake_name("QA", role)}.png'
      path.write_bytes(f'QA:{role}:stage2'.encode('ascii'))
      maps[role] = str(path.resolve())
    stale = self.texture_dir / self.core.source_map_bake_name('Stale', 'BaseColor')
    stale.with_suffix('.png').write_bytes(b'stale-scan-must-not-be-used')

    package_maps = {'QA': maps} if maps else {}
    snapshot_dir = self.base / '_painter_archive' / 'QA' / '10_bake_baseline_once'
    contract_path = snapshot_dir / 'contract' / 'painter_package.json'
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(
      json.dumps({
        'contract': 'meshy-painter-package-v1',
        'painter_texture_sets': ['QA'],
        'source_map_texture_sets': sorted(package_maps),
        'map_roles': {
          texture_set: sorted(role_paths)
          for texture_set, role_paths in sorted(package_maps.items())
        },
        'fbx': {'low': low_fbx.name, 'high': high_fbx.name},
        'resolution': 512,
        'normal_convention': 'DIRECTX',
        'normal_basis': 'LOW_TANGENT',
      }, sort_keys=True),
      encoding='utf-8',
    )

    logical_paths = {
      'contract/painter_package.json': contract_path,
      f'low/{low_fbx.name}': low_fbx,
      f'high/{high_fbx.name}': high_fbx,
      **{f'texture/{Path(path).name}': Path(path) for path in maps.values()},
    }
    manifest = {
      'kind': 'substance_tools_immutable_snapshot_set',
      'schema_version': 1,
      'files': [
        {
          'path': logical,
          'source': self._identity(path),
          'backup': self._identity(path),
        }
        for logical, path in sorted(
          logical_paths.items(),
          key=lambda item: item[0].casefold(),
        )
      ],
    }
    manifest_path = snapshot_dir / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    pins = manifest['files']
    state = {
      'stage': 'BAKE_BASELINE_ARCHIVED',
      'archive': {
        'source_original': {'verified': 'mocked'},
        'bake_baseline': {
          'snapshot_dir': str(snapshot_dir.resolve()),
          'snapshot_manifest': str(manifest_path.resolve()),
          'snapshot_manifest_sha256': self.core.file_hash(manifest_path),
          'snapshot_entries': pins,
          'snapshot_files': len(pins),
          'resolution': 512,
        },
      },
      'painter_package': {
        'painter_texture_sets': ['QA'],
        'maps': package_maps,
        'fbx': {
          'low': str(low_fbx.resolve()),
          'high': str(high_fbx.resolve()),
        },
        'normal_convention': 'DIRECTX',
        'normal_basis': 'LOW_TANGENT',
        'resolution': 512,
      },
    }
    return state, manifest, maps

  def _verified_plan(self, state, manifest, texture_sets=('QA',)):
    with mock.patch(
      'substance_tools.meshy_pipeline.load_pipeline_state',
      return_value=state,
    ), mock.patch(
      'substance_tools.meshy_pipeline.verify_source_archive_receipt',
    ) as verify_stage1, mock.patch(
      'substance_tools.meshy_pipeline_contract.verify_immutable_snapshot_set_archive',
      return_value=manifest,
    ):
      plan = self.operators.verified_meshy_painter_source_plans(
        bpy.context.scene,
        list(texture_sets),
        self.texture_dir,
      )
    verify_stage1.assert_called_once_with(state['archive']['source_original'])
    return plan

  def test_plans_use_only_state_and_exact_stage2_pins(self):
    state, manifest, maps = self._state_fixture(include_normal=True)
    plan = self._verified_plan(state, manifest)

    self.assertEqual(
      set(plan['source_material_maps']['QA']),
      {'BaseColor', 'ExtraR', 'Roughness', 'Metallic'},
    )
    self.assertNotIn('Extra', plan['source_material_maps']['QA'])
    self.assertEqual(
      plan['source_normal_mesh_maps']['QA']['source_normal_texture'],
      maps['Normal'],
    )
    self.assertEqual(
      plan['canonical_output_roles'],
      {'QA': ['Color', 'Extra', 'Normal']},
    )
    self.assertEqual(plan['fbx'], state['painter_package']['fbx'])
    self.assertNotIn('Stale', plan['source_material_maps'])

  def test_changed_working_map_is_rejected_even_when_archive_is_valid(self):
    state, manifest, maps = self._state_fixture(include_normal=True)
    Path(maps['Roughness']).write_bytes(b'changed-after-stage2')
    with self.assertRaisesRegex(RuntimeError, 'differs from stage-2'):
      self._verified_plan(state, manifest)

  def test_optional_base_color_and_normal_without_extra_are_supported(self):
    state, manifest, maps = self._state_fixture(
      include_normal=True,
      material_roles=['BaseColor'],
    )
    plan = self._verified_plan(state, manifest)
    self.assertEqual(plan['source_material_maps'], {
      'QA': {'BaseColor': maps['BaseColor']},
    })
    self.assertEqual(
      plan['source_normal_mesh_maps']['QA']['source_normal_texture'],
      maps['Normal'],
    )
    self.assertEqual(plan['canonical_output_roles'], {'QA': ['Color', 'Normal']})

  def test_partial_extra_transport_is_rejected(self):
    state, manifest, _maps = self._state_fixture(
      include_normal=False,
      material_roles=['BaseColor', 'Extra', 'Roughness'],
    )
    with self.assertRaisesRegex(RuntimeError, 'complete or absent'):
      self._verified_plan(state, manifest)

  def test_texture_set_with_no_source_maps_is_omitted(self):
    state, manifest, _maps = self._state_fixture(
      include_normal=False,
      material_roles=[],
    )
    state['painter_package']['maps'] = {}
    plan = self._verified_plan(state, manifest)
    self.assertEqual(plan['source_material_maps'], {})
    self.assertEqual(plan['source_normal_mesh_maps'], {})
    self.assertEqual(plan['canonical_output_roles'], {'QA': []})

  def test_manifest_sha_pin_is_required(self):
    state, manifest, _maps = self._state_fixture(include_normal=False)
    state['archive']['bake_baseline']['snapshot_manifest_sha256'] = '0' * 64
    with self.assertRaisesRegex(RuntimeError, 'SHA-256 pin'):
      self._verified_plan(state, manifest)

  def test_state_ids_that_collide_in_painter_are_rejected(self):
    state, manifest, maps = self._state_fixture(include_normal=True)
    state['painter_package']['maps'] = {
      'Fabric-Red': maps,
      'Fabric_Red': maps,
    }
    state['painter_package']['painter_texture_sets'] = [
      'Fabric-Red',
      'Fabric_Red',
    ]
    with self.assertRaisesRegex(RuntimeError, 'Painter name collisions'):
      self._verified_plan(
        state,
        manifest,
        texture_sets=('Fabric-Red', 'Fabric_Red'),
      )

  def test_success_receipt_matches_every_expected_set_channel_and_normal(self):
    state, manifest, _maps = self._state_fixture(include_normal=True)
    plan = self._verified_plan(state, manifest)
    material_maps = plan['source_material_maps']
    normal_maps = plan['source_normal_mesh_maps']
    source_digest = self.operators._source_material_plan_digest(
      material_maps['QA']
    )
    normal_sha256 = self.core.file_hash(
      Path(normal_maps['QA']['source_normal_texture'])
    )
    normal_resource_name = f'ST_M_QA_SourceNormal_{normal_sha256[:12]}'
    request = {
      'meshy_contract_version': 1,
      'strict_bake_settings': True,
      'source_material_maps': material_maps,
      'source_material_hashes': self.core.hash_nested_existing_paths(material_maps),
      'source_normal_mesh_maps': normal_maps,
      'source_normal_mesh_hashes': self.core.hash_nested_existing_paths(normal_maps),
      'bake_settings_result': {
        'contract': 'meshy-bake-settings-v1',
        'strict': True,
        'exact': True,
        'requested': {
          'antialiasing': 'X2',
          'match': 'BY_MESH_NAME',
          'id_source': 'MATERIAL_COLOR',
        },
        'configured_texture_set_count': 1,
        'texture_sets': {
          'M_QA': {
            'configured': True,
            'set_call_succeeded': True,
            'antialiasing': 'X2',
            'match': 'BY_MESH_NAME',
            'id_source': 'MATERIAL_COLOR',
            'resolution': 512,
          },
        },
      },
      'source_layer_result': {
        'managed_layer_count': 2,
        'texture_sets': {
          'M_QA': {
            'channels': ['BaseColor', 'ExtraR', 'Metallic', 'Roughness'],
            'digest': source_digest,
            'layers': 2,
            'result': 'created',
          },
        },
      },
      'source_normal_mesh_map_result': {
        'assigned_texture_sets': ['M_QA'],
        'assigned_count': 1,
        'normal_baker_omitted_texture_sets': ['QA'],
        'normal_baker_omitted_count': 1,
        'assignments': {
          'M_QA': {
            'source_sha256': normal_sha256,
            'resource_name': normal_resource_name,
            'resource_identity': f'resource://{normal_resource_name}',
          },
        },
      },
    }
    receipt = self.operators.validate_meshy_painter_source_receipts(
      request,
      material_maps,
      normal_maps,
      plan['canonical_texture_sets'],
      plan['resolution'],
    )
    self.assertEqual(receipt['texture_sets'], ['QA'])
    self.assertEqual(receipt['managed_layer_count'], 2)
    self.assertEqual(receipt['source_normal_texture_sets'], ['QA'])

    request['source_layer_result']['managed_layer_count'] = 1
    with self.assertRaisesRegex(RuntimeError, 'total differs'):
      self.operators.validate_meshy_painter_source_receipts(
        request,
        material_maps,
        normal_maps,
        plan['canonical_texture_sets'],
        plan['resolution'],
      )

  def test_changed_low_texture_sets_are_rejected_after_stage2(self):
    state, manifest, _maps = self._state_fixture(include_normal=True)
    with self.assertRaisesRegex(RuntimeError, 'differ from the stage-2 pin'):
      self._verified_plan(state, manifest, texture_sets=('QA', 'Added'))

  def test_export_source_state_contract_is_exact(self):
    state, manifest, _maps = self._state_fixture(include_normal=True)
    plan = self._verified_plan(state, manifest)
    expected = self.operators.meshy_expected_source_state(plan)
    self.assertEqual(expected['contract'], 'meshy-source-state-v1')
    self.assertEqual(expected['canonical_texture_sets'], ['QA'])
    self.assertEqual(
      expected['material']['QA']['digest'],
      self.operators._source_material_plan_digest(
        plan['source_material_maps']['QA']
      ),
    )
    normal_sha256 = self.core.file_hash(Path(
      plan['source_normal_mesh_maps']['QA']['source_normal_texture']
    ))
    self.assertEqual(
      expected['normal']['QA'],
      {
        'source_sha256': normal_sha256,
        'resource_name': f'ST_M_QA_SourceNormal_{normal_sha256[:12]}',
      },
    )
    receipt = dict(expected)
    receipt['exact'] = True
    self.assertEqual(
      self.operators.validate_meshy_export_source_state_receipt(
        {'source_state_receipt': receipt},
        expected,
      ),
      receipt,
    )
    changed = json.loads(json.dumps(receipt))
    changed['material']['QA']['digest'] = '0' * 64
    with self.assertRaisesRegex(RuntimeError, 'export-time source'):
      self.operators.validate_meshy_export_source_state_receipt(
        {'source_state_receipt': changed},
        expected,
      )

  def test_receipt_rejects_settings_digest_and_normal_identity_mismatch(self):
    state, manifest, _maps = self._state_fixture(include_normal=True)
    plan = self._verified_plan(state, manifest)
    material_maps = plan['source_material_maps']
    normal_maps = plan['source_normal_mesh_maps']
    digest = self.operators._source_material_plan_digest(material_maps['QA'])
    normal_sha256 = self.core.file_hash(
      Path(normal_maps['QA']['source_normal_texture'])
    )
    resource_name = f'ST_M_QA_SourceNormal_{normal_sha256[:12]}'
    request = {
      'meshy_contract_version': 1,
      'strict_bake_settings': True,
      'source_material_maps': material_maps,
      'source_material_hashes': self.core.hash_nested_existing_paths(material_maps),
      'source_normal_mesh_maps': normal_maps,
      'source_normal_mesh_hashes': self.core.hash_nested_existing_paths(normal_maps),
      'bake_settings_result': {
        'contract': 'meshy-bake-settings-v1',
        'strict': True,
        'exact': True,
        'requested': {
          'antialiasing': 'X2',
          'match': 'BY_MESH_NAME',
          'id_source': 'MATERIAL_COLOR',
        },
        'configured_texture_set_count': 1,
        'texture_sets': {
          'M_QA': {
            'configured': True,
            'set_call_succeeded': True,
            'antialiasing': 'X2',
            'match': 'BY_MESH_NAME',
            'id_source': 'MATERIAL_COLOR',
            'resolution': 512,
          },
        },
      },
      'source_layer_result': {
        'managed_layer_count': 1,
        'texture_sets': {
          'M_QA': {
            'channels': sorted(material_maps['QA']),
            'digest': digest,
            'layers': 1,
            'result': 'created',
          },
        },
      },
      'source_normal_mesh_map_result': {
        'assigned_texture_sets': ['M_QA'],
        'assigned_count': 1,
        'normal_baker_omitted_texture_sets': ['M_QA'],
        'normal_baker_omitted_count': 1,
        'assignments': {
          'M_QA': {
            'source_sha256': normal_sha256,
            'resource_name': resource_name,
            'resource_identity': f'resource://{resource_name}',
          },
        },
      },
    }

    request['bake_settings_result']['texture_sets']['M_QA']['match'] = 'NAME'
    with self.assertRaisesRegex(RuntimeError, 'labels differ'):
      self.operators.validate_meshy_painter_source_receipts(
        request, material_maps, normal_maps, plan['canonical_texture_sets'],
        plan['resolution']
      )
    request['bake_settings_result']['texture_sets']['M_QA']['match'] = 'BY_MESH_NAME'
    request['source_layer_result']['texture_sets']['M_QA']['digest'] = '0' * 64
    with self.assertRaisesRegex(RuntimeError, 'layer digest differs'):
      self.operators.validate_meshy_painter_source_receipts(
        request, material_maps, normal_maps, plan['canonical_texture_sets'],
        plan['resolution']
      )
    request['source_layer_result']['texture_sets']['M_QA']['digest'] = digest
    request['source_normal_mesh_map_result']['assignments']['M_QA'][
      'resource_identity'
    ] = 'resource://wrong'
    with self.assertRaisesRegex(RuntimeError, 'resource identity differs'):
      self.operators.validate_meshy_painter_source_receipts(
        request, material_maps, normal_maps, plan['canonical_texture_sets'],
        plan['resolution']
      )

  def test_uv_ready_export_entry_is_read_only_and_rejected(self):
    low = self._low_object()
    request_path = self.texture_dir / self.core.PAINTER_EXPORT_REQUEST
    result_path = self.texture_dir / self.core.PAINTER_EXPORT_RESULT
    request_path.write_bytes(b'keep-request')
    result_path.write_bytes(b'keep-result')
    before = (request_path.read_bytes(), result_path.read_bytes())
    state = {'stage': 'UV_READY'}

    with mock.patch(
      'substance_tools.meshy_pipeline.load_pipeline_state',
      return_value=state,
    ):
      with self.assertRaisesRegex(RuntimeError, 'requires BAKE_BASELINE_ARCHIVED'):
        self.operators.validate_meshy_export_apply_entry(
          bpy.context.scene,
          [low],
          self.texture_dir,
          result_path,
        )

    bpy.ops.wm.save_as_mainfile(
      filepath=str(self.base / 'stage_gate.blend'),
      check_existing=False,
    )
    with mock.patch.object(
      self.operators,
      'baking_paths',
      return_value={'texture_dir': self.texture_dir},
    ), mock.patch(
      'substance_tools.meshy_pipeline.load_pipeline_state',
      return_value=state,
    ):
      with self.assertRaisesRegex(RuntimeError, 'UV_READY'):
        bpy.ops.st.export_painter_textures_and_apply()

    self.assertEqual(
      (request_path.read_bytes(), result_path.read_bytes()),
      before,
    )

  def test_terminal_stage_is_exact_noop_and_rejects_changed_new_export(self):
    low = self._low_object()
    state, manifest, _maps = self._state_fixture(include_normal=True)
    incoming = {}
    canonical = {}
    for role, rgba in {
      'Color': (0.2, 0.4, 0.6, 1.0),
      'Extra': (0.1, 0.3, 0.8, 1.0),
      'Normal': (0.5, 0.25, 1.0, 1.0),
    }.items():
      incoming[role] = self.texture_dir / f'M_QA_{role}.png'
      canonical[role] = self.texture_dir / f'T_QA_{role}.png'
      _write_png(incoming[role], rgba)
    apply_receipt = self.core.apply_painter_export_transaction(
      {'textures': {'QA': [str(path) for path in incoming.values()]}},
      [low],
      self.texture_dir,
      meshy_mode=True,
      canonical_texture_sets=['QA'],
    )
    state['stage'] = 'CANONICAL_APPLIED'
    state['checkpoints'] = {
      'CANONICAL_APPLIED': {
        'applied_materials': apply_receipt['applied'],
        'texture_dir': str(self.texture_dir.resolve()),
        'managed_roles': apply_receipt['managed_roles'],
        'canonical_files': [
          {
            'path': str(Path(path).resolve()),
            'size': Path(path).stat().st_size,
            'sha256': self.core.file_hash(Path(path)),
          }
          for path in apply_receipt['canonical_files']
        ],
      },
    }
    result_path = self.texture_dir / self.core.PAINTER_EXPORT_RESULT

    patches = (
      mock.patch(
        'substance_tools.meshy_pipeline.load_pipeline_state',
        return_value=state,
      ),
      mock.patch('substance_tools.meshy_pipeline.verify_source_archive_receipt'),
      mock.patch(
        'substance_tools.meshy_pipeline_contract.verify_immutable_snapshot_set_archive',
        return_value=manifest,
      ),
    )
    with patches[0], patches[1], patches[2]:
      decision = self.operators.validate_meshy_export_apply_entry(
        bpy.context.scene,
        [low],
        self.texture_dir,
        result_path,
      )
      self.assertEqual(decision['action'], 'NOOP')

      for role in incoming:
        incoming[role].write_bytes(canonical[role].read_bytes())
      result_path.write_text(json.dumps({
        'status': 'SUCCESS',
        'textures': {'QA': [str(path) for path in incoming.values()]},
      }), encoding='utf-8')
      same_bytes = result_path.read_bytes()
      decision = self.operators.validate_meshy_export_apply_entry(
        bpy.context.scene,
        [low],
        self.texture_dir,
        result_path,
      )
      self.assertEqual(decision['action'], 'NOOP')
      self.assertEqual(result_path.read_bytes(), same_bytes)

      incoming['Normal'].write_bytes(b'changed-new-export')
      with self.assertRaisesRegex(RuntimeError, 'changed Painter export'):
        self.operators.validate_meshy_export_apply_entry(
          bpy.context.scene,
          [low],
          self.texture_dir,
          result_path,
        )
      self.assertEqual(result_path.read_bytes(), same_bytes)

  def test_meshy_send_maps_is_read_only_noop_and_preserves_receipts(self):
    self._low_object()
    state, manifest, _maps = self._state_fixture(include_normal=True)
    texture_request = self.texture_dir / self.core.PAINTER_REQUEST
    low_request = self.low_dir / self.core.PAINTER_REQUEST
    texture_request.write_bytes(b'preserve-texture-receipt')
    low_request.write_bytes(b'preserve-low-receipt')
    before = (texture_request.read_bytes(), low_request.read_bytes())
    bpy.ops.wm.save_as_mainfile(
      filepath=str(self.base / 'send_maps_noop.blend'),
      check_existing=False,
    )
    paths = {
      'texture_dir': self.texture_dir,
      'low_dir': self.low_dir,
      'spp': self.base / 'intentionally_missing.spp',
    }
    with mock.patch.object(
      self.operators,
      'baking_paths',
      return_value=paths,
    ), mock.patch.object(
      self.operators,
      'get_preferences',
      side_effect=AssertionError('preferences must not be read for Meshy no-op'),
    ), mock.patch(
      'substance_tools.meshy_pipeline.load_pipeline_state',
      return_value=state,
    ), mock.patch(
      'substance_tools.meshy_pipeline.verify_source_archive_receipt',
    ), mock.patch(
      'substance_tools.meshy_pipeline_contract.verify_immutable_snapshot_set_archive',
      return_value=manifest,
    ):
      self.assertEqual(bpy.ops.st.send_painter_maps(), {'FINISHED'})

    self.assertEqual(
      (texture_request.read_bytes(), low_request.read_bytes()),
      before,
    )


if __name__ == '__main__':
  unittest.main(argv=[__file__], verbosity=2)

"""Ordinary UPDATE must not trust a bake plan written for other inputs (#13).

The actual ``ExportBakingToSubstancePainterOperator`` and the actual
``load_bake_plan``/``bake_plan_stale_reasons`` are executed from source with
fake Blender data and fake native/export functions, so no Blender or Painter
is needed and nothing outside a temporary folder is written.
"""

import ast
import hashlib
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE_FUNCTIONS = ('read_json', 'write_json', 'load_bake_plan', '_same_path', 'bake_plan_stale_reasons')


def _core_functions():
  tree = ast.parse((ROOT / 'core.py').read_text(encoding='utf-8'))
  nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in CORE_FUNCTIONS]
  namespace = dict(json=json, os=os, Path=Path)
  exec(compile(ast.Module(body=nodes, type_ignores=[]), 'core.py', 'exec'), namespace)
  return {name: namespace[name] for name in CORE_FUNCTIONS}


def _operator_class():
  tree = ast.parse((ROOT / 'operators.py').read_text(encoding='utf-8'))
  return next(node for node in tree.body
              if isinstance(node, ast.ClassDef) and node.name == 'ExportBakingToSubstancePainterOperator')


def mesh(name, texture_set, content='v1'):
  obj = types.SimpleNamespace(name=name, name_full=name, texture_set=texture_set, content=content)
  obj.as_pointer = lambda: id(obj)
  return obj


class Scene(dict):
  """Blender scenes take ID properties by item and settings by attribute."""


def fake_hash(objects, **_kwargs):
  data = sorted((obj.name, obj.content) for obj in objects)
  return hashlib.sha256(json.dumps(data).encode('utf-8')).hexdigest()


def by_texture_set(objects):
  groups = {}
  for obj in objects:
    groups.setdefault(obj.texture_set, []).append(obj)
  return groups


def base(name, suffix):
  return name.lower().replace('_' + suffix, '')


SETTINGS_PROPS = dict(resolution='2048', antialiasing='NONE', match='BY_MESH_NAME',
                      id_source='MATERIAL', painter_low_hide_solidify_rim=True)


class Project:
  """One .blend in a folder whose texture directory may be shared."""

  def __init__(self, folder, asset, low, high=()):
    self.folder = Path(folder)
    self.asset = asset
    self.blend = self.folder / f'{asset}.blend'
    self.low = list(low)
    self.high = list(high)
    self.props = types.SimpleNamespace(**SETTINGS_PROPS)
    self.paths = {
      'asset': asset,
      'low_dir': self.folder / 'low',
      'high_dir': self.folder / 'high',
      'texture_dir': self.folder / 'texture',
      'low_fbx': self.folder / 'low' / f'{asset}_low.fbx',
      'high_fbx': self.folder / 'high' / f'{asset}_high.fbx',
      'spp': self.folder / 'texture' / f'{asset}_SP.spp',
      'bake_plan': self.folder / 'texture' / '.substance_tools_bake_plan.json',
    }
    for key in ('low_dir', 'high_dir', 'texture_dir'):
      self.paths[key].mkdir(parents=True, exist_ok=True)
    self.paths['spp'].write_bytes(b'spp')
    self.blend.write_bytes(b'blend')  # also stands in for the Painter executable
    self.paths['low_fbx'].write_bytes(b'fbx')


class StaleOrdinaryBakePlanTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.folder = Path(self.tmp.name)
    self.core = _core_functions()
    self.shoes = Project(self.folder, 'wear_sibuki_shoes_01',
                         [mesh('Knot_low', 'wear_sibuki_shoes_01_Knot'),
                          mesh('Sole_low', 'wear_sibuki_shoes_01_Sole')],
                         [mesh('Knot_high', 'wear_sibuki_shoes_01_Knot')])
    self.ribbon = Project(self.folder, 'Cloth_ornament_sibuki_02',
                          [mesh('strip_low', 'Cloth_sibuki_ornament_strip_01')])
    self.meshy_contract = None
    self.source_maps = {}

  # -- fake Blender add-on world -------------------------------------------------
  def run_update(self, project):
    native = mock.Mock(name='native')
    native.painter_is_running.return_value = True
    native.workstation.preflight_pending.return_value = None
    reports = []

    class Operator:
      def report(self, kind, message):
        reports.append((sorted(kind)[0], message))

    bpy = types.SimpleNamespace(
      types=types.SimpleNamespace(Operator=Operator),
      props=types.SimpleNamespace(StringProperty=lambda **kw: None, EnumProperty=lambda **kw: None),
      data=types.SimpleNamespace(filepath=str(project.blend)),
    )
    scene = Scene()
    scene.substance_tools_baking = project.props
    context = types.SimpleNamespace(scene=scene)
    collections = ('Baking', 'low', 'high', 'alpha')

    def painter_collection_meshes(collection):
      return {'low': project.low, 'high': project.high, 'alpha': []}[collection]

    def high_entries(low_objects, high_objects, high_dir, asset):
      result = []
      for texture_set, lows in sorted(by_texture_set(low_objects).items()):
        bases = {base(obj.name, 'low') for obj in lows}
        highs = [obj for obj in high_objects if base(obj.name, 'high') in bases]
        if highs:
          fbxs = [high_dir / f'{obj.name}.fbx' for obj in highs]
          result.append(dict(texture_set=texture_set, bases=sorted(bases), objects=highs,
                             fbx=fbxs[0] if len(fbxs) == 1 else None, fbxs=fbxs))
      return result

    namespace = dict(
      bpy=bpy, workstation=native.workstation, Path=Path, time=time, json=json,
      hashlib=hashlib, traceback=mock.Mock(),
      baking_paths=lambda: project.paths,
      pending_request_path=lambda: self.folder / 'pending.json',
      painter_is_running=native.painter_is_running,
      launch_painter=native.launch_painter,
      get_preferences=lambda context: {'painter_path': str(project.blend)},
      ensure_baking_collections=lambda scene: collections,
      painter_collection_meshes=painter_collection_meshes,
      low_as_high_texture_set_names=lambda low, high: sorted(
        set(by_texture_set(low)) - {entry['texture_set'] for entry in high_entries(low, high, Path(), '')}),
      unmatched_mesh_names=lambda low, high: ([], []),
      unreal_template_path=lambda path: Path(path),
      low_texture_set_names=lambda objects: sorted(by_texture_set(objects)),
      verified_meshy_painter_source_plans=lambda scene, sets, directory: self.meshy_contract,
      validate_meshy_bake_request_settings=lambda settings: None,
      back_normal_mesh_map_plan=lambda sets, directory: {},
      PAINTER_REQUEST='.substance_tools_request.json',
      fast_content_hash=fake_hash,
      low_objects_by_texture_set=by_texture_set,
      high_entries_by_texture_set=high_entries,
      match_base=base,
      file_hash=lambda path: 'file:' + str(path),
      export_objects_to_fbx=native.export_objects_to_fbx,
      painter_source_map_plan=lambda sets, directory: self.source_maps,
      painter_source_normal_mesh_map_plan=lambda sets, directory: {},
      alpha_color_bake_name=lambda texture_set: texture_set + '_alpha',
      hash_existing_back_normal_sources=lambda maps: {},
      hash_nested_existing_paths=lambda maps: json.loads(json.dumps(maps)),
      __name__='substance_stale_plan_test.operators', __package__='substance_stale_plan_test',
    )
    namespace.update(self.core)
    exec(compile(ast.Module(body=[_operator_class()], type_ignores=[]), 'operators.py', 'exec'), namespace)
    operator = namespace['ExportBakingToSubstancePainterOperator']()
    operator.action = 'UPDATE'
    operator.workstation_phase_id = 'phase-ribbon'
    result = operator.execute(context)
    return result, reports, native

  def settings_hash(self, project):
    # Same settings dictionary the operator hashes for an ordinary project.
    props = project.props
    sets = sorted(by_texture_set(project.low))
    paired = set()
    for texture_set, lows in by_texture_set(project.low).items():
      bases = {base(obj.name, 'low') for obj in lows}
      if any(base(obj.name, 'high') in bases for obj in project.high):
        paired.add(texture_set)
    settings = {
      'resolution': int(props.resolution), 'antialiasing': props.antialiasing, 'match': props.match,
      'cage': 'AUTOMATIC', 'id_source': props.id_source,
      'painter_low_hide_solidify_rim': props.painter_low_hide_solidify_rim,
      'low_as_high_texture_sets': sorted(set(sets) - paired), 'back_normal_mesh_maps': {},
      'mesh_maps': ['Normal', 'WorldSpaceNormal', 'ID', 'AO', 'Curvature', 'Position', 'Thickness'],
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode('utf-8')).hexdigest()

  def clean_plan(self, project, *, with_spp=True):
    """The plan a successful request for ``project`` leaves in the texture folder."""
    low_hashes = {ts: fake_hash(objs) for ts, objs in by_texture_set(project.low).items()}
    high_hashes = {}
    for texture_set, lows in by_texture_set(project.low).items():
      bases = {base(obj.name, 'low') for obj in lows}
      highs = [obj for obj in project.high if base(obj.name, 'high') in bases]
      if highs:
        high_hashes[texture_set] = fake_hash(highs)
    plan = {
      'version': 1, 'blend_file': str(project.blend.resolve()),
      'texture_sets': sorted(by_texture_set(project.low)),
      'low_hash': hashlib.sha256(json.dumps(low_hashes, sort_keys=True).encode('utf-8')).hexdigest(),
      'low_hashes': low_hashes, 'low_changed': False, 'low_baseline_missing': False,
      'changed_low_texture_sets': [], 'high_hashes': high_hashes, 'changed_high_texture_sets': [],
      'changed_back_normal_texture_sets': [], 'back_normal_hashes': {}, 'rebake_texture_sets': [],
      'reload_only_texture_sets': [], 'settings_hash': self.settings_hash(project), 'settings_changed': False,
    }
    if with_spp:
      plan['spp'] = str(project.paths['spp'].resolve())
    return plan

  def write_plan(self, project, plan):
    self.core['write_json'](project.paths['bake_plan'], plan)
    previous = dict(plan, spp=str(project.paths['spp'].resolve()), source_material_hashes={},
                    source_normal_mesh_hashes={}, base_color_hashes={}, alpha_color_hashes={})
    self.core['write_json'](project.paths['texture_dir'] / '.substance_tools_request.json', previous)
    return project.paths['bake_plan'].read_bytes()

  def assert_rejected_before_native(self, project, result, reports, native, plan_bytes, expected):
    self.assertEqual(result, {'CANCELLED'})
    self.assertEqual(reports[-1][0], 'ERROR')
    self.assertIn('Checked bake plan is stale', reports[-1][1])
    self.assertIn(expected, reports[-1][1])
    self.assertIn('Nothing was exported or sent to Painter', reports[-1][1])
    native.export_objects_to_fbx.assert_not_called()
    native.launch_painter.assert_not_called()
    native.workstation.publish_request_copies.assert_not_called()
    native.workstation.publish_pending.assert_not_called()
    native.workstation.attach_phase.assert_not_called()
    self.assertEqual(project.paths['bake_plan'].read_bytes(), plan_bytes)

  # -- scenarios -----------------------------------------------------------------
  def test_shared_texture_folder_plan_from_other_project_is_rejected(self):
    # Reproduces 1791036622466845700: the shoes plan was used for the ribbon SPP.
    plan_bytes = self.write_plan(self.shoes, self.clean_plan(self.shoes))
    result, reports, native = self.run_update(self.ribbon)
    self.assert_rejected_before_native(self.ribbon, result, reports, native, plan_bytes, 'wear_sibuki_shoes_01.blend')
    self.assertIn('wear_sibuki_shoes_01_Knot', reports[-1][1])

  def test_renamed_texture_sets_in_the_same_project_are_rejected(self):
    plan = self.clean_plan(self.ribbon)
    self.ribbon.low = [mesh('strip_low', 'Cloth_sibuki_ornament_strip_02')]
    plan_bytes = self.write_plan(self.ribbon, plan)
    result, reports, native = self.run_update(self.ribbon)
    self.assert_rejected_before_native(self.ribbon, result, reports, native, plan_bytes, 'differ from current')

  def test_low_change_after_check_is_rejected_instead_of_skipping_the_reload(self):
    plan_bytes = self.write_plan(self.ribbon, self.clean_plan(self.ribbon))
    self.ribbon.low = [mesh('strip_low', 'Cloth_sibuki_ornament_strip_01', content='edited')]
    result, reports, native = self.run_update(self.ribbon)
    self.assert_rejected_before_native(self.ribbon, result, reports, native, plan_bytes, 'Low meshes changed')

  def test_high_change_after_check_is_rejected(self):
    plan_bytes = self.write_plan(self.shoes, self.clean_plan(self.shoes))
    self.shoes.high = [mesh('Knot_high', 'wear_sibuki_shoes_01_Knot', content='resculpted')]
    result, reports, native = self.run_update(self.shoes)
    self.assert_rejected_before_native(self.shoes, result, reports, native, plan_bytes, 'High meshes changed')

  def test_settings_change_after_check_is_rejected(self):
    plan_bytes = self.write_plan(self.ribbon, self.clean_plan(self.ribbon))
    self.ribbon.props.resolution = '4096'
    result, reports, native = self.run_update(self.ribbon)
    self.assert_rejected_before_native(self.ribbon, result, reports, native, plan_bytes, 'bake settings changed')

  def test_plan_for_another_spp_is_rejected(self):
    plan = self.clean_plan(self.ribbon)
    plan['spp'] = str(self.shoes.paths['spp'].resolve())
    plan_bytes = self.write_plan(self.ribbon, plan)
    result, reports, native = self.run_update(self.ribbon)
    self.assert_rejected_before_native(self.ribbon, result, reports, native, plan_bytes, 'targets')

  def test_matching_fresh_plan_is_used(self):
    for with_spp in (True, False):  # plans written before this fix have no spp field
      with self.subTest(with_spp=with_spp):
        self.write_plan(self.ribbon, self.clean_plan(self.ribbon, with_spp=with_spp))
        self.source_maps = {'Cloth_sibuki_ornament_strip_01': {'BaseColor': f'color-{with_spp}.png'}}
        result, reports, native = self.run_update(self.ribbon)
        self.assertEqual(result, {'FINISHED'}, reports)
        native.workstation.attach_phase.assert_called_once()
        request = native.workstation.publish_request_copies.call_args.args[1]
        self.assertTrue(request['bake_plan_used'])
        self.assertEqual(request['spp'], str(self.ribbon.paths['spp'].resolve()))
        self.assertEqual(sorted(request['low_hashes']), ['Cloth_sibuki_ornament_strip_01'])
        self.assertEqual(request['rebake_texture_sets'], [])
        native.export_objects_to_fbx.assert_not_called()  # unchanged Low with an existing FBX
        written = self.core['read_json'](self.ribbon.paths['bake_plan'])
        self.assertEqual(written['spp'], str(self.ribbon.paths['spp'].resolve()))

  def test_unchanged_fresh_plan_still_reports_no_work(self):
    self.write_plan(self.ribbon, self.clean_plan(self.ribbon))
    result, reports, native = self.run_update(self.ribbon)
    self.assertEqual(result, {'FINISHED'})
    self.assertIn('No checked bake changes', reports[-1][1])
    native.workstation.publish_request_copies.assert_not_called()
    native.export_objects_to_fbx.assert_not_called()

  def test_missing_plan_keeps_the_existing_rejection(self):
    result, reports, native = self.run_update(self.ribbon)
    self.assertEqual(result, {'CANCELLED'})
    self.assertEqual(reports[-1][1], 'Run Check Bake Plan before Update Painter')
    native.export_objects_to_fbx.assert_not_called()
    native.workstation.publish_request_copies.assert_not_called()

  def test_meshy_texture_set_guard_is_unchanged(self):
    plan = self.clean_plan(self.ribbon)
    plan['texture_sets'] = ['other_state_id']
    self.write_plan(self.ribbon, plan)
    self.meshy_contract = {'fbx': {'low': 'low.fbx', 'high': 'high.fbx'},
                           'source_material_maps': {}, 'source_normal_mesh_maps': {}}
    result, reports, native = self.run_update(self.ribbon)
    self.assertEqual(result, {'CANCELLED'})
    self.assertIn('Checked bake plan Texture Sets differ from Meshy state IDs', reports[-1][1])
    native.export_objects_to_fbx.assert_not_called()
    native.workstation.publish_request_copies.assert_not_called()


class BakePlanStaleReasonsTests(unittest.TestCase):
  def setUp(self):
    self.reasons = _core_functions()['bake_plan_stale_reasons']
    self.plan = {'blend_file': 'C:/Art/A.blend', 'spp': 'C:/Art/texture/A_SP.spp',
                 'texture_sets': ['B', 'A'], 'settings_hash': 's', 'low_hashes': {'A': '1'}, 'high_hashes': {}}

  def check(self, **overrides):
    values = dict(blend_file='c:\\art\\a.blend' if os.name == 'nt' else 'C:/Art/A.blend',
                  spp='C:/Art/texture/A_SP.spp', texture_sets=['A', 'B'], settings_hash='s',
                  low_hashes={'A': '1'}, high_hashes={})
    values.update(overrides)
    return self.reasons(self.plan, **values)

  def test_matching_plan_has_no_reasons(self):
    self.assertEqual(self.check(), [])
    self.assertEqual(self.check(low_hashes=None, high_hashes=None), [])

  def test_each_mismatch_is_named(self):
    self.assertIn('written by', self.check(blend_file='C:/Art/B.blend')[0])
    self.assertIn('targets', self.check(spp='C:/Art/texture/B_SP.spp')[0])
    self.assertIn('Texture Sets', self.check(texture_sets=['A'])[0])
    self.assertEqual(self.check(settings_hash='t'), ['bake settings changed'])
    self.assertEqual(self.check(low_hashes={'A': '2'}), ['Low meshes changed'])
    self.assertEqual(self.check(high_hashes={'A': 'h'}), ['High meshes changed'])

  def test_plan_without_blend_identity_is_stale(self):
    self.plan.pop('blend_file')
    self.assertIn('unknown .blend', self.check()[0])


if __name__ == '__main__':
  unittest.main()

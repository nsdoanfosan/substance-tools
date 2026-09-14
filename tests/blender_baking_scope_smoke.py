"""Factory-startup test: scopes isolate real collections without saving preferences."""
import importlib.util
import pathlib
import sys
import types
import bpy

root = pathlib.Path(__file__).resolve().parents[1]
package = types.ModuleType('scope_test_addon')
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + '.core', root / 'core.py')
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)

legacy = bpy.context.scene
legacy_root, legacy_low, *_ = core.ensure_baking_collections(legacy)
mesh = bpy.data.meshes.new('preserved_mesh')
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new('preserved', mesh)
legacy_low.objects.link(obj)
scenes = []
for name in ('Plate_04', 'Plate_02'):
    scene = bpy.data.scenes.new(name)
    receipt = core.configure_baking_scope(name, scene=scene)
    root, low, high, alpha = core.get_baking_collections(scene)
    assert root.name == receipt['root']
    assert root is not legacy_root
    assert obj.name not in root.all_objects
    assert core.baking_paths(scene)['spp'].name == name + '_SP.spp'
    scoped_obj = bpy.data.objects.new(name + '_low', mesh.copy())
    low.objects.link(scoped_obj)
    scenes.append((scene, root, low, scoped_obj))
assert scenes[0][1] is not scenes[1][1]
assert scenes[0][3].name not in scenes[1][1].all_objects
assert core.get_baking_collections(legacy)[0] is legacy_root
assert obj.name in legacy_low.objects and len(mesh.polygons) == 1
for invalid in ('', '../bad', 'bad/name', 'bad.name'):
    try:
        core.configure_baking_scope(invalid, scene=scenes[0][0])
    except ValueError:
        pass
    else:
        raise AssertionError(invalid)
scenes[0][0]['_substance_tools_meshy_pipeline_state_v2'] = '{"stage":"VERIFIED"}'
try:
    core.configure_baking_scope('Other', scene=scenes[0][0])
except RuntimeError:
    pass
else:
    raise AssertionError('Prepared pipeline was allowed to change scope')
try:
    core.configure_baking_scope('Plate_02', scene=bpy.data.scenes.new('Collision'))
except RuntimeError:
    pass
else:
    raise AssertionError('Another scene reused an owned root')
print('BAKING_SCOPE_SMOKE_PASS')

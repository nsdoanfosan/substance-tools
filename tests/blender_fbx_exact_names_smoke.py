import bpy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import substance_tools.core as core

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add()
source = bpy.context.object
source.name = 'wear_test_low'
original_mesh = source.data
path = Path(tempfile.mkdtemp(prefix='st_fbx_names_')) / 'pair.fbx'
core.export_objects_to_fbx([source], path)
assert source.name == 'wear_test_low'
assert source.data is original_mesh
assert bpy.context.view_layer.objects.active == source
assert not any(o.name.startswith('__SubstanceToolsExport') for o in bpy.data.objects)
# Remove the source only from this disposable factory-startup test, so the
# imported FBX model name can be inspected without Blender adding a suffix.
bpy.data.objects.remove(source, do_unlink=True)
bpy.ops.import_scene.fbx(filepath=str(path))
names = [o.name for o in bpy.context.selected_objects if o.type == 'MESH']
assert names == ['wear_test_low'], names
print('EXACT_FBX_NAMES_PASS', names)

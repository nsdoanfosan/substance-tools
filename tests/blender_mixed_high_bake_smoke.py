"""Mixed material set must use its detailed high plus self-bake fallback parts."""
import bpy,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import substance_tools.core as core
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
mat=bpy.data.materials.new('M_mixed')
def cube(name):
    bpy.ops.mesh.primitive_cube_add();o=bpy.context.object;o.name=name;o.data.materials.append(mat);return o
a=cube('cloth_low');b=cube('button_low_003');h=cube('cloth_high_01')
assert core.match_base('ribbon_low_s','low')==core.match_base('ribbon_high_s','high')=='ribbon'
assert core.low_as_high_texture_set_names([a,b],[h])==[]
assert core.low_as_high_texture_set_names([b],[])==['mixed']
with tempfile.TemporaryDirectory(prefix='st_mixed_high_') as temp:
    entry=core.high_entries_by_texture_set([a,b],[h],Path(temp),'test')[0]
    assert set(entry['export_names'])=={'cloth_high_01','button_high_003'}
    assert entry['fallback_objects']=={b}
    path=Path(temp)/'button.fbx'
    core.export_objects_to_fbx([b],path,export_names=['button_high_003'])
    assert b.name=='button_low_003'
    bpy.ops.import_scene.fbx(filepath=str(path))
    assert [o.name for o in bpy.context.selected_objects]==['button_high_003']
    assert not any(o.name.startswith('__SubstanceToolsExport') for o in bpy.data.objects)
print('MIXED_HIGH_AND_FBX_ALIAS_PASS')

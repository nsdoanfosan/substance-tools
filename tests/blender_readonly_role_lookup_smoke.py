"""UI queries must work with untagged legacy collections and never migrate IDs."""
import sys
from pathlib import Path
import bpy
sys.path.insert(0, str(Path.home() / 'AppData/Roaming/Blender Foundation/Blender/5.2/scripts/addons'))
from substance_tools import core

root = bpy.data.collections.new(core.BAKING_COLLECTION)
bpy.context.scene.collection.children.link(root)
low = bpy.data.collections.new(core.LOW_COLLECTION)
root.children.link(low)
export = bpy.data.collections.new(core.SEND2UE_EXPORT_COLLECTION)
bpy.context.scene.collection.children.link(export)
mesh = bpy.data.meshes.new('test_mesh')
obj = bpy.data.objects.new('test_low', mesh)
low.objects.link(obj)
export.objects.link(obj)
material = bpy.data.materials.new('M_test')
mesh.materials.append(material)
alpha = bpy.data.objects.new('test_alpha', mesh)

assert core.find_baking_role_child(root, core.LOW_COLLECTION) == low
assert core.COLLECTION_ROLE_PROPERTY not in low
assert core.export_status_groups()[0] == (obj,)
assert core.COLLECTION_ROLE_PROPERTY not in low
collection_names = set(bpy.data.collections.keys())
assert core.alpha_target_material_items(alpha, None)[1][0] == material.name
assert set(bpy.data.collections.keys()) == collection_names
assert core.COLLECTION_ROLE_PROPERTY not in low

# Write permission exists only for the explicit preparation path.
assert core.ensure_baking_role_child(root, core.LOW_COLLECTION) == low
assert low[core.COLLECTION_ROLE_PROPERTY] == core.LOW_COLLECTION
low.name = 'Renamed production low'
assert core.find_baking_role_child(root, core.LOW_COLLECTION) == low

# Missing hierarchy queries must not manufacture collections from a UI callback.
bpy.context.scene['st_baking_root_name'] = 'missing_scope'
assert core.alpha_target_material_items(alpha, None) == [('AUTO', 'Auto', 'Use the only or same-named Low material')]
assert set(bpy.data.collections.keys()) - {'Renamed production low'} == collection_names - {core.LOW_COLLECTION}
print('READONLY_ROLE_LOOKUP_PASS')

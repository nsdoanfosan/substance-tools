"""Role transitions remain correct without scanning unrelated object memberships."""
import addon_utils
import bpy
from types import SimpleNamespace
assert addon_utils.enable('substance_tools', default_set=False)
from substance_tools import core
scene = bpy.context.scene
_, low, high, alpha = core.ensure_baking_collections(scene)
obj = bpy.data.objects.new('RoleTransition', bpy.data.meshes.new('RoleTransition'))
scene.collection.objects.link(obj)
core.remember_baking_role_membership(scene)
updates = SimpleNamespace(updates=[SimpleNamespace(id=obj)])
assert not core.depsgraph_requires_baking_role_sync(scene, updates)
low.objects.link(obj)
assert core.depsgraph_requires_baking_role_sync(scene, updates)
core.sync_exclusive_baking_roles(scene)
assert core.object_baking_roles(obj, core.baking_role_children(scene)[1]) == {core.LOW_COLLECTION}
high.objects.link(obj)
core.sync_exclusive_baking_roles(scene)
assert obj.name not in low.objects and obj.name in high.objects
assert not core.depsgraph_requires_baking_role_sync(scene, updates)
high.objects.unlink(obj)
assert core.depsgraph_requires_baking_role_sync(scene, updates)
core.sync_exclusive_baking_roles(scene)
assert not core.depsgraph_requires_baking_role_sync(scene, updates)
# A transform/selection notification must never query users_collection.
old = core.object_baking_roles
def forbidden(*args): raise AssertionError('Unrelated object scan')
core.object_baking_roles = forbidden
for i in range(100):
    assert not core.depsgraph_requires_baking_role_sync(scene, updates)
core.object_baking_roles = old
print('BAKING_ROLE_UPDATES_PASS', flush=True)

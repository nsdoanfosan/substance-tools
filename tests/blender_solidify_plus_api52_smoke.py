import addon_utils
import bpy


MODULE = "substance_tools"


addon_utils.enable(MODULE, default_set=False)
try:
    from substance_tools import core

    mesh = bpy.data.meshes.new("Substance_SolidifyPlusMesh")
    mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [],
        [(0, 1, 2)],
    )
    mesh.update()
    obj = bpy.data.objects.new("Substance_SolidifyPlusObject", mesh)
    bpy.context.scene.collection.objects.link(obj)

    node_group = bpy.data.node_groups.new("Solidify Plus API52", "GeometryNodeTree")
    node_group.interface.new_socket(
        name="Geometry",
        in_out="OUTPUT",
        socket_type="NodeSocketGeometry",
    )
    node_group.nodes.new("NodeGroupOutput")
    fill_rim_socket = node_group.interface.new_socket(
        name="Fill Rim",
        in_out="INPUT",
        socket_type="NodeSocketBool",
    )
    modifier = obj.modifiers.new("Solidify Plus API52", "NODES")
    modifier.node_group = node_group
    input_group = modifier.properties.inputs[fill_rim_socket.identifier]
    input_group["value"] = True

    restore = core.set_solidify_plus_fill_rim([obj], False)
    assert input_group["value"] is False
    core.restore_solidify_plus_fill_rim(restore)
    assert input_group["value"] is True

    del input_group["value"]
    assert core.geometry_nodes_input_state(modifier, fill_rim_socket.identifier) == (
        False,
        None,
    )
    missing_restore = core.set_solidify_plus_fill_rim([obj], True)
    assert input_group["value"] is True
    core.restore_solidify_plus_fill_rim(missing_restore)
    assert "value" not in input_group
    input_group["value"] = True

    true_hash = core.fast_content_hash([obj])
    normalized_true_hash = core.fast_content_hash(
        [obj], normalize_solidify_plus_fill_rim=True
    )
    assert input_group["value"] is True
    input_group["value"] = False
    false_hash = core.fast_content_hash([obj])
    normalized_false_hash = core.fast_content_hash(
        [obj], normalize_solidify_plus_fill_rim=True
    )
    assert input_group["value"] is False
    assert true_hash != false_hash
    assert normalized_true_hash == normalized_false_hash

    class LegacyModifier(dict):
        pass

    legacy_modifier = LegacyModifier(Socket_7=True)
    assert core.geometry_nodes_input_state(legacy_modifier, "Socket_7") == (True, True)
    core.set_geometry_nodes_input_value(legacy_modifier, "Socket_7", False)
    assert legacy_modifier["Socket_7"] is False
    core.delete_geometry_nodes_input_value(legacy_modifier, "Socket_7")
    assert "Socket_7" not in legacy_modifier

    print("SUBSTANCE_SOLIDIFY_PLUS_API52_SMOKE_OK")
finally:
    addon_utils.disable(MODULE, default_set=False)

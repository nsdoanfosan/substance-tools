import addon_utils
import bpy


MODULE = "substance_tools"


def make_mesh_object(name, display_type, collection, parent=None):
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [],
        [(0, 1, 2)],
    )
    obj = bpy.data.objects.new(name, mesh)
    obj.display_type = display_type
    obj.parent = parent
    collection.objects.link(obj)
    return obj


addon_utils.enable(MODULE, default_set=False)
try:
    from substance_tools.core import (
        collection_meshes,
        ensure_baking_collections,
        is_painter_guide_mesh,
        painter_collection_meshes,
        painter_low_export_hierarchy,
    )

    _, low_collection, _, _ = ensure_baking_collections(bpy.context.scene)

    textured_parent = bpy.data.objects.new("textured_parent", None)
    low_collection.objects.link(textured_parent)
    textured = make_mesh_object(
        "guide_name_is_not_a_marker",
        "TEXTURED",
        low_collection,
        parent=textured_parent,
    )

    wire_parent = bpy.data.objects.new("wire_parent", None)
    low_collection.objects.link(wire_parent)
    wire = make_mesh_object(
        "production_name_is_not_an_override",
        "WIRE",
        low_collection,
        parent=wire_parent,
    )

    assert set(collection_meshes(low_collection)) == {textured, wire}
    assert painter_collection_meshes(low_collection) == [textured]
    assert not is_painter_guide_mesh(textured)
    assert is_painter_guide_mesh(wire)

    hierarchy = painter_low_export_hierarchy()
    assert textured in hierarchy
    assert textured_parent in hierarchy
    assert wire not in hierarchy
    assert wire_parent not in hierarchy
finally:
    addon_utils.disable(MODULE, default_set=False)

print("PAINTER_WIRE_GUIDE_SMOKE_OK")

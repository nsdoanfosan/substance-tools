import types
import unittest
from test_painter_source_maps import _load_plugin_with_import_stubs


class AuthoredIDTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.painter = _load_plugin_with_import_stubs()
        class Layer:
            def mask_effects(self): return []
            def content_effects(self): return []
        class Group(Layer):
            def sub_layers(self): return self.children
        class Selection:
            def get_parameters(self): return types.SimpleNamespace(id_mask=self.resource)
        self.Layer, self.Group, self.Selection = Layer, Group, Selection
        self.painter.layerstack.LayerNode = Layer
        self.painter.layerstack.GroupLayerNode = Group
        self.painter.layerstack.ColorSelectionEffectNode = Selection
        self.painter.textureset.MeshMapUsage = types.SimpleNamespace(ID='ID')
        self.resource = types.SimpleNamespace(context='project0',name='id Socks',version='new')
        self.texture = types.SimpleNamespace(get_mesh_map_resource=lambda _:self.resource,all_stacks=lambda:[0])

    def test_preserves_id_referenced_inside_nested_mask_even_after_version_change(self):
        effect=self.Selection();effect.resource=types.SimpleNamespace(context='project0',name='id Socks',version='old')
        child=self.Layer();child.mask_effects=lambda:[effect]
        root=self.Group();root.children=[child]
        self.painter.layerstack.get_root_layer_nodes=lambda _:[root]
        self.assertIs(self.plugin._authored_id_map_to_preserve(self.texture),self.resource)

    def test_unrelated_custom_mask_does_not_disable_id_bake(self):
        effect=self.Selection();effect.resource=types.SimpleNamespace(context='project0',name='custom Mask')
        root=self.Layer();root.mask_effects=lambda:[effect]
        self.painter.layerstack.get_root_layer_nodes=lambda _:[root]
        self.assertIsNone(self.plugin._authored_id_map_to_preserve(self.texture))

    def test_new_project_without_id_keeps_normal_bake_path(self):
        self.texture.get_mesh_map_resource=lambda _:None
        self.assertIsNone(self.plugin._authored_id_map_to_preserve(self.texture))


if __name__=='__main__': unittest.main()

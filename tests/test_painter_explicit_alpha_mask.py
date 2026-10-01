import tempfile
import types
import unittest
from pathlib import Path

from test_painter_source_maps import _load_plugin_with_import_stubs


class ExplicitAlphaMaskTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.sp = _load_plugin_with_import_stubs()
        self.plugin._log = lambda message: None
        self.roots, self.imports = [], []

        class Fill:
            def __init__(self):
                self.name, self.sources, self.effects, self.mask = '', {}, [], False
            def get_name(self): return self.name
            def set_name(self, name): self.name = name
            def set_source(self, channel, resource): self.sources[channel] = resource
            def set_projection_mode(self, mode): self.projection = mode
            def has_mask(self): return self.mask
            def add_mask(self, background): self.mask = True
            def mask_effects(self): return self.effects

        class Effect(Fill): pass

        stack = object()
        texture_set = types.SimpleNamespace(name='Paper', all_stacks=lambda: [stack])
        self.sp.textureset.all_texture_sets = lambda: [texture_set]
        self.sp.textureset.ChannelType = types.SimpleNamespace(BaseColor='base_color')
        ls = self.sp.layerstack
        ls.FillLayerNode, ls.FillEffectNode = Fill, Effect
        ls.ProjectionMode = types.SimpleNamespace(UV='uv')
        ls.MaskBackground = types.SimpleNamespace(Black='black')
        ls.NodeStack = types.SimpleNamespace(Mask='mask')
        ls.get_root_layer_nodes = lambda stack: list(self.roots)
        ls.InsertPosition = types.SimpleNamespace(
            from_textureset_stack=lambda stack: ('root', stack),
            inside_node=lambda node, stack: ('mask', node),
        )
        def insert(position):
            node = Fill() if position[0] == 'root' else Effect()
            (self.roots if position[0] == 'root' else position[1].effects).append(node)
            return node
        ls.insert_fill = insert
        self.sp.resource.Usage = types.SimpleNamespace(TEXTURE='texture', ALPHA='alpha')
        def import_resource(path, usage, **kwargs):
            self.imports.append((path, usage))
            return types.SimpleNamespace(identifier=lambda: (path, usage))
        self.sp.resource.import_project_resource = import_resource

    def test_explicit_mask_keeps_color_and_coverage_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            color, mask = Path(folder) / 'rgba.png', Path(folder) / 'coverage.png'
            color.touch(); mask.touch()
            request = {'alpha_color_maps': {'Paper': str(color)},
                       'alpha_mask_maps': {'Paper': str(mask)}}
            self.plugin._apply_alpha_color_layers(request)
            self.assertEqual(self.imports, [(str(color), 'texture'), (str(mask), 'alpha')])
            layer = self.roots[0]
            self.assertEqual(layer.active_channels, {'base_color'})
            self.assertEqual(layer.sources['base_color'], (str(color), 'texture'))
            self.assertEqual(layer.effects[0].sources[None], (str(mask), 'alpha'))
            self.plugin._apply_alpha_color_layers(request)
            self.assertEqual(len(self.roots), 1)
            self.assertEqual(len(layer.effects), 1)

    def test_missing_later_mask_rejects_whole_plan_before_import(self):
        with tempfile.TemporaryDirectory() as folder:
            color = Path(folder) / 'rgba.png'; color.touch()
            request = {'alpha_color_maps': {'Paper': str(color), 'Other': str(color)},
                       'alpha_mask_maps': {'Other': str(Path(folder) / 'missing.png')}}
            with self.assertRaisesRegex(RuntimeError, 'Explicit Alpha mask'):
                self.plugin._apply_alpha_color_layers(request)
            self.assertEqual(self.imports, [])
            self.assertEqual(self.roots, [])

    def test_legacy_request_uses_same_image_for_mask(self):
        with tempfile.TemporaryDirectory() as folder:
            color = Path(folder) / 'legacy.png'; color.touch()
            self.plugin._apply_alpha_color_layers({'alpha_color_maps': {'Paper': str(color)}})
            self.assertEqual(self.imports, [(str(color), 'texture'), (str(color), 'alpha')])


if __name__ == '__main__':
    unittest.main()

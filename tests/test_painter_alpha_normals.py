import tempfile
import types
import unittest
from pathlib import Path
from test_painter_source_maps import _load_plugin_with_import_stubs


class AlphaNormalLayerTests(unittest.TestCase):
    def setUp(self):
        self.plugin,self.sp=_load_plugin_with_import_stubs()
        self.plugin._log=lambda message:None
        self.roots=[]
        self.imports=[]
        class Fill:
            def __init__(self): self.name=''; self.sources={}; self.effects=[]; self.active_channels=set(); self.mask=False
            def get_name(self): return self.name
            def set_name(self,n): self.name=n
            def set_visible(self,value): self.visible=value
            def set_opacity(self,value,channel): self.opacity=(value,channel)
            def set_source(self,c,r): self.sources[c]=r
            def set_blending_mode(self,b,c): self.blending=(b,c)
            def set_projection_mode(self,p): self.projection=p
            def has_mask(self): return self.mask
            def add_mask(self,b): self.mask=True
            def mask_effects(self): return self.effects
        class Effect(Fill): pass
        self.Fill=Fill
        channels={'base_color'}
        self.stack=types.SimpleNamespace(has_channel=lambda c:c in channels, add_channel=lambda c,f:channels.add(c))
        self.texture_set=types.SimpleNamespace(name='Shirt',all_stacks=lambda:[self.stack])
        self.sp.textureset.all_texture_sets=lambda:[self.texture_set]
        self.sp.textureset.ChannelType=types.SimpleNamespace(BaseColor='base_color',Normal='normal')
        self.sp.textureset.ChannelFormat=types.SimpleNamespace(RGB16='rgb16')
        self.sp.layerstack.FillLayerNode=Fill
        self.sp.layerstack.FillEffectNode=Effect
        self.sp.layerstack.ProjectionMode=types.SimpleNamespace(UV='uv')
        self.sp.layerstack.BlendingMode=types.SimpleNamespace(NormalMapDetail='rnm')
        self.sp.layerstack.MaskBackground=types.SimpleNamespace(Black='black')
        self.sp.layerstack.NodeStack=types.SimpleNamespace(Mask='mask')
        self.sp.layerstack.get_root_layer_nodes=lambda s:list(self.roots)
        self.sp.layerstack.InsertPosition=types.SimpleNamespace(from_textureset_stack=lambda s:('root',s),inside_node=lambda n,s:('mask',n))
        def insert(pos):
            node=Fill() if pos[0]=='root' else Effect()
            (self.roots if pos[0]=='root' else pos[1].effects).append(node)
            return node
        self.sp.layerstack.insert_fill=insert
        self.sp.resource.Usage=types.SimpleNamespace(TEXTURE='texture',ALPHA='alpha')
        def import_resource(path,usage,**kw):
            self.imports.append((path,usage))
            return types.SimpleNamespace(identifier=lambda: (path,usage))
        self.sp.resource.import_project_resource=import_resource

    def test_companion_normal_uses_own_mask_and_removal_reuses_layer(self):
        with tempfile.TemporaryDirectory() as folder:
            color=Path(folder)/'color.png'; color.write_bytes(b'color')
            normal=Path(folder)/'normal.png'; normal.write_bytes(b'normal')
            request={'alpha_color_maps':{'Shirt':str(color)},'alpha_normal_maps':{'Shirt':str(normal)}}
            self.plugin._apply_alpha_color_layers(request)
            by_name={x.name:x for x in self.roots}
            color_layer=by_name['Blender Alpha Details']; normal_layer=by_name['Blender Alpha Normals']
            self.assertEqual(color_layer.active_channels,{'base_color'})
            self.assertEqual(normal_layer.active_channels,{'normal'})
            self.assertEqual(normal_layer.blending,('rnm','normal'))
            self.assertEqual(normal_layer.effects[0].sources[None],(str(normal),'alpha'))
            self.assertEqual(normal_layer.sources['normal'],(str(normal),'texture'))
            self.plugin._apply_alpha_color_layers(request)
            self.assertEqual(len(self.roots),2)
            self.assertEqual(len(normal_layer.effects),1)
            self.plugin._apply_alpha_color_layers({'alpha_color_maps':{'Shirt':str(color)},'alpha_normal_maps':{}})
            self.assertEqual(normal_layer.active_channels,set())
            self.assertEqual(color_layer.active_channels,{'base_color'})
            self.plugin._apply_alpha_color_layers(request)
            self.assertEqual(normal_layer.active_channels,{'normal'})
            self.assertEqual(len(self.roots),2)

    def test_missing_requested_normal_fails_before_mutation(self):
        with self.assertRaises(RuntimeError):
            self.plugin._apply_alpha_color_layers({'alpha_color_maps':{'Shirt':'color.png'},'alpha_normal_maps':{'Shirt':'missing.png'}})
        self.assertEqual(self.roots,[])
        self.assertEqual(self.imports,[])

    def test_scoped_normal_update_preserves_unrelated_set_and_uses_explicit_mask(self):
        outside=types.SimpleNamespace(name='Skirt',all_stacks=lambda:[object()])
        self.sp.textureset.all_texture_sets=lambda:[outside,self.texture_set]
        original=self.sp.layerstack.get_root_layer_nodes
        self.sp.layerstack.get_root_layer_nodes=lambda stack: original(stack) if stack is self.stack else self.fail('Touched unrelated set')
        with tempfile.TemporaryDirectory() as folder:
            normal=Path(folder)/'normal.png'; normal.write_bytes(b'normal')
            mask=Path(folder)/'coverage.png'; mask.write_bytes(b'grayscale coverage')
            self.plugin._apply_alpha_detail_channel({'managed_alpha_texture_sets':['Shirt'],
                'alpha_normal_maps':{'Shirt':str(normal)},'alpha_normal_mask_maps':{'Shirt':str(mask)}},normal=True)
            layer=self.roots[0]
            self.assertEqual(layer.active_channels,{'normal'})
            self.assertTrue(layer.visible)
            self.assertEqual(layer.opacity,(1.0,'normal'))
            self.assertEqual(layer.effects[0].sources[None],(str(mask),'alpha'))

    def test_explicit_grayscale_mask_keeps_color_resource_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            color=Path(folder)/'color.png';color.write_bytes(b'rgba')
            mask=Path(folder)/'mask.png';mask.write_bytes(b'alpha')
            self.plugin._apply_alpha_color_layers({'alpha_color_maps':{'Shirt':str(color)},'alpha_mask_maps':{'Shirt':str(mask)}})
            layer=self.roots[0]
            self.assertEqual(layer.sources['base_color'],(str(color),'texture'))
            self.assertEqual(layer.effects[0].sources[None],(str(mask),'alpha'))

    def test_explicit_missing_mask_fails_before_import(self):
        with tempfile.TemporaryDirectory() as folder:
            color=Path(folder)/'color.png';color.write_bytes(b'rgba')
            with self.assertRaises(RuntimeError):
                self.plugin._apply_alpha_color_layers({'alpha_color_maps':{'Shirt':str(color)},'alpha_mask_maps':{'Shirt':str(Path(folder)/'missing.png')}})
            self.assertEqual(self.roots,[])
            self.assertEqual(self.imports,[])

    def test_legacy_color_request_does_not_touch_normal_layer(self):
        old=self.Fill();old.name='Blender Alpha Normals';old.active_channels={'normal'};self.roots.append(old)
        self.plugin._apply_alpha_color_layers({'alpha_color_maps':{}})
        self.assertEqual(old.active_channels,{'normal'})

    def test_retired_id_only_set_disables_only_managed_detail_layers(self):
        for name in ('Blender Alpha Details','Blender Alpha Normals','Existing Smart Material'):
            node=self.Fill();node.name=name;node.active_channels={'normal','base_color'};self.roots.append(node)
        self.plugin._apply_alpha_color_layers({'alpha_color_maps':{},'alpha_normal_maps':{},
                                             'retired_alpha_texture_sets':['Shirt']})
        self.assertEqual([node.active_channels for node in self.roots],
                         [set(),set(),{'normal','base_color'}])

    def test_normal_only_update_applies_without_mesh_reload_or_bake(self):
        p=self.plugin
        request={'request_id':'alpha-normal-change','action':'UPDATE',
                 'low_changed':False,'changed_high_texture_sets':[],
                 'settings_changed':False,'rebake_texture_sets':[],
                 'alpha_normal_changed':True}
        p._started=True; p._processing=False; p._last_polled_pipeline_hash=None
        p._load_request=lambda:request
        p._request_matches_saved_metadata=lambda metadata,request:False
        p._log_timing=lambda *args:None
        self.sp.project.is_busy=lambda:False
        self.sp.project.Metadata=lambda context:{}
        scheduled=[]
        p._execute_when_not_busy_guarded=lambda callback,request:scheduled.append(callback.__name__)
        p._start_bake=lambda request:self.fail('Detail normal update triggered mesh-map baking')
        self.sp.project.reload_mesh=lambda *args:self.fail('Detail normal update reloaded mesh')
        p._on_project_ready()
        self.assertEqual(scheduled,['_save_reimported_request'])
        self.assertFalse(request['_needs_bake'])


if __name__=='__main__': unittest.main()

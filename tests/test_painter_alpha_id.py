"""Native ID refresh preserves existing paint and validates every source first."""
import contextlib
import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class AlphaIDTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.png = self.folder / 'id.png'
        self.png.write_bytes(b'verified source')
        self.spp = self.folder / 'cloth.spp'
        self.imports = []
        class Effect:
            def __init__(self, name='old paint', uid=8): self.name=name; self.id=uid
            def uid(self): return self.id
            def get_name(self): return self.name
            def set_name(self, value): self.name=value
            def set_parameters(self, value): self.params=value
            def get_parameters(self): return self.params
            def set_blending_mode(self, value): self.mode=value
            def set_opacity(self, value): self.opacity=value
        class Layer:
            def __init__(self): self.effects=[Effect()]; self.mask=True
            def uid(self): return 42
            def get_name(self): return 'buttons'
            def has_mask(self): return self.mask
            def add_mask(self, value): self.mask=True
            def mask_effects(self): return list(self.effects)
        self.layer=Layer()
        layers=types.ModuleType('substance_painter.layerstack')
        layers.GroupLayerNode=type('Group', (), {})
        layers.ColorSelectionEffectNode=Effect
        layers.get_root_layer_nodes=lambda stack:[self.layer]
        layers.ScopedModification=lambda name:contextlib.nullcontext()
        layers.InsertPosition=types.SimpleNamespace(above_node=lambda node:node,inside_node=lambda layer,kind:layer)
        def insert(position):
            effect=Effect(uid=9);self.layer.effects.insert(0,effect);return effect
        layers.insert_color_selection_effect=insert
        layers.ColorSelectionEffectParams=lambda **kw:types.SimpleNamespace(**kw)
        layers.MaskBackground=types.SimpleNamespace(Black='black')
        layers.ColorSelectionBackgroundColor=types.SimpleNamespace(Black='black')
        layers.NodeStack=types.SimpleNamespace(Mask='mask')
        layers.BlendingMode=types.SimpleNamespace(Normal='normal')
        project=types.ModuleType('substance_painter.project')
        project.is_open=lambda:True;project.is_busy=lambda:False
        project.is_in_edition_state=lambda:True;project.file_path=lambda:str(self.spp)
        resources=types.ModuleType('substance_painter.resource')
        resources.Usage=types.SimpleNamespace(TEXTURE='texture')
        def import_resource(path, usage):
            self.imports.append(path)
            return types.SimpleNamespace(identifier=lambda:'native-id')
        resources.import_project_resource=import_resource
        sets=types.ModuleType('substance_painter.textureset')
        sets.TextureSet=types.SimpleNamespace(from_name=lambda name:types.SimpleNamespace(get_stack=lambda:object()))
        colors=types.ModuleType('substance_painter.colormanagement')
        self.working=object()
        colors.GenericColorSpace=types.SimpleNamespace(Working=self.working)
        colors.Color=lambda *rgb,**kw:(rgb,kw['color_space'])
        parent=types.ModuleType('substance_painter');parent.__path__=[]
        modules={m.__name__:m for m in (parent,layers,project,resources,sets,colors)}
        for m in (layers,project,resources,sets,colors):setattr(parent,m.__name__.rsplit('.',1)[1],m)
        self.patch=mock.patch.dict(sys.modules,modules);self.patch.start();self.addCleanup(self.patch.stop)
        path=Path(__file__).resolve().parents[1]/'painter/startup/substance_tools_unreal_viewport/alpha_id.py'
        spec=importlib.util.spec_from_file_location('test_alpha_id_native',path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.binding={'texture_set_name':'Jacket','layer_uid':42,'expected_layer_name':'buttons',
                      'effect_name':'source ID','colors':[[0,0,1]],'texture_path':str(self.png),
                      'expected_sha256':hashlib.sha256(self.png.read_bytes()).hexdigest()}

    def request(self, *bindings):
        return {'spp':str(self.spp),'alpha_id_selections':list(bindings or [self.binding])}

    def test_refresh_uses_working_enum_and_preserves_paint_without_duplicate_effects(self):
        old=self.layer.effects[0]
        receipt=self.module.apply_id_selections(self.request())[0]
        effect=self.layer.effects[0]
        self.assertEqual(receipt['preserved_previous_effect_uids'],[old.uid()])
        self.assertIs(self.layer.effects[1],old)
        self.assertEqual(effect.params.colors,[((0,0,1),self.working)])
        self.assertEqual((effect.mode,effect.opacity),('normal',1))
        self.module.apply_id_selections(self.request())
        self.assertEqual(len(self.layer.effects),2)
        self.assertIs(self.layer.effects[0],effect)

    def test_later_invalid_source_cannot_mutate_earlier_target(self):
        second=dict(self.binding,effect_name='another',expected_sha256='bad')
        with self.assertRaisesRegex(ValueError,'PNG bytes'):
            self.module.apply_id_selections(self.request(self.binding,second))
        self.assertEqual(self.imports,[])
        self.assertEqual(len(self.layer.effects),1)

    def test_wrong_project_layer_name_and_duplicate_targets_are_rejected(self):
        requests=[dict(self.request(),spp=str(self.folder/'other.spp')),
                  self.request(dict(self.binding,expected_layer_name='changed')),
                  self.request(self.binding,self.binding)]
        for request in requests:
            with self.subTest(request=request),self.assertRaises(ValueError):
                self.module.apply_id_selections(request)
        self.assertEqual(self.imports,[])

    def test_nonfinite_palette_is_rejected_before_import(self):
        with self.assertRaises(ValueError):
            self.module.apply_id_selections(self.request(dict(self.binding,colors=[[float('nan'),0,1]])))
        self.assertEqual(self.imports,[])


if __name__=='__main__':unittest.main()

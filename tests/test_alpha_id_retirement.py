import ast
import json
import tempfile
import unittest
from pathlib import Path


class RetirementTests(unittest.TestCase):
    def test_only_id_only_set_maps_retire_and_regular_sources_clear_retirement(self):
        source=Path(__file__).resolve().parents[1]/'core.py'
        names={'alpha_color_bake_name','alpha_normal_bake_name','retire_id_only_detail_maps'}
        functions=[n for n in ast.parse(source.read_text(encoding='utf-8')).body
                   if isinstance(n,ast.FunctionDef) and n.name in names]
        ns={'Path':Path,'clean_name':lambda s:s,
            'read_json':lambda p,d:json.loads(p.read_text()) if p.exists() else d,
            'write_json':lambda p,d:p.write_text(json.dumps(d))}
        exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),ns)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            retired_files=['T_Shirt_Color_alpha.png','T_Shirt_Normal_alpha.png','T_Shirt_NormalMask_alpha.png']
            # Use the real core name helper to match the persisted bake contract.
            retired_files[0]=ns['alpha_color_bake_name']('Shirt')+'.png'
            for name in retired_files+['unrelated.png']:(root/name).write_bytes(b'old')
            retire=ns['retire_id_only_detail_maps']
            self.assertEqual(retire(root,{}, {'Shirt':'id.png'}),['Shirt'])
            self.assertTrue(all(not (root/name).exists() for name in retired_files))
            self.assertEqual((root/'unrelated.png').read_bytes(),b'old')
            self.assertEqual(retire(root,{},{}),['Shirt'])
            self.assertEqual(retire(root,{'Shirt':'new.png'},{'Shirt':'id.png'}),[])


if __name__=='__main__':unittest.main()

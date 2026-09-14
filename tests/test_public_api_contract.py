import ast
import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
API_PATH = REPO / 'api.py'


class PublicApiSourceContractTests(unittest.TestCase):

  def test_painter_facade_is_versioned_and_exports_only_declared_work(self):
    tree = ast.parse(API_PATH.read_text(encoding='utf-8'))
    functions = {
      node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assignments = {}
    for node in tree.body:
      if not isinstance(node, ast.Assign):
        continue
      for target in node.targets:
        if isinstance(target, ast.Name):
          try:
            assignments[target.id] = ast.literal_eval(node.value)
          except (ValueError, TypeError):
            pass

    contract = json.loads(
      (REPO / 'pipeline_contract.json').read_text(encoding='utf-8')
    )['integration_apis']['painter_transfer']
    functions_declared = set(contract['functions'])

    self.assertEqual(assignments['PAINTER_TRANSFER_API_VERSION'], 1)
    self.assertEqual(
      assignments['PAINTER_TRANSFER_SERVICE_ID'],
      'substance.painter_transfer',
    )
    self.assertEqual(contract['version'], 1)
    self.assertEqual(contract['module'], 'substance_tools.api')
    self.assertEqual(contract['getter'], 'get_painter_transfer_api')
    self.assertTrue(functions_declared.issubset(functions))
    self.assertTrue(functions_declared.issubset(set(assignments['__all__'])))

  def test_facade_has_no_quad_remesher_or_uvgami_implementation(self):
    source = API_PATH.read_text(encoding='utf-8')
    self.assertIn('adopt_retopology_pair as _adopt_retopology_pair', source)
    self.assertIn('_resolve_low_export_api', source)
    self.assertNotIn('analyze_mesh_object', source)
    self.assertNotIn('launch_uvgami_low_uv', source)
    self.assertNotIn('confirm_uvgami_low_uv', source)
    self.assertNotIn('sync_painter_export', source)
    self.assertNotIn('.objects.link(', source)
    self.assertNotIn("bpy.data.collections.new('Export')", source)


if __name__ == '__main__':
  unittest.main()

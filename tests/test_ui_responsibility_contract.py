import ast
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UI_PATH = REPO / 'ui.py'


class UiResponsibilityContractTests(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.source = UI_PATH.read_text(encoding='utf-8')
    cls.tree = ast.parse(cls.source)

  def _class_assignments(self, class_name):
    node = next(
      item
      for item in self.tree.body
      if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    values = {}
    for item in node.body:
      if not isinstance(item, ast.Assign):
        continue
      for target in item.targets:
        if isinstance(target, ast.Name):
          values[target.id] = ast.literal_eval(item.value)
    return node, values

  def test_existing_panel_remains_primary_and_meshy_transfer_is_late_closed(self):
    _, primary = self._class_assignments('SubstanceToolsPanel')
    _, meshy = self._class_assignments('SubstanceToolsMeshyPainterPanel')
    self.assertEqual(meshy['bl_parent_id'], primary['bl_idname'])
    self.assertIn('DEFAULT_CLOSED', meshy['bl_options'])
    self.assertGreaterEqual(meshy['bl_order'], 100)

  def test_substance_panel_exposes_only_painter_owned_meshy_actions(self):
    node, _ = self._class_assignments('SubstanceToolsMeshyPainterPanel')
    panel_source = ast.get_source_segment(self.source, node)
    self.assertIn('st.bake_meshy_source_maps', panel_source)
    self.assertIn('st.verify_meshy_pipeline', panel_source)
    self.assertNotIn('st.prepare_meshy_retopo', panel_source)
    self.assertNotIn('st.adopt_retopology_pair', panel_source)
    self.assertNotIn('st.finalize_meshy_retopo', panel_source)
    self.assertNotIn('st.prepare_meshy_low_uv', panel_source)


if __name__ == '__main__':
  unittest.main()

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_original_revision_test'
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg

def load(name):
    spec = importlib.util.spec_from_file_location(PACKAGE + '.' + name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

original = load('original_source_revision')
maps = load('source_revision')

class Scene(dict):
    name = 'Rebuilt source'

class OriginalRevisionTests(unittest.TestCase):
    def test_originals_and_baselines_are_disjoint_between_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            asset = Path(folder).resolve() / 'Asset'
            old = asset / '00_source_original_once'
            first = asset / '00_source_original_revisions' / 'first_remake'
            second = asset / '00_source_original_revisions' / 'second_remake'
            self.assertEqual(maps.active_archive_parent({}, old), asset)
            a = maps.active_archive_parent({}, first)
            b = maps.active_archive_parent({}, second)
            self.assertNotEqual(a, b)
            self.assertFalse(a.is_relative_to(first))
            self.assertFalse(a.is_relative_to(old))
            self.assertEqual(maps.revision_parent(first, 'fixed_projection').parent, a / 'source_map_revisions')

    def test_rejects_unsafe_or_unknown_archive_paths(self):
        for token in ('../bad', 'a/b', '', '.', 'x' * 81):
            with self.assertRaises(ValueError): original.validate_revision_id(token)
        with self.assertRaises(ValueError): original.original_stage_parent(Path('unknown'))

    def test_active_repair_cannot_change_original(self):
        stub = types.ModuleType(PACKAGE + '.meshy_pipeline')
        stub.load_pipeline_state = lambda scene: scene.get('state', {})
        sys.modules[stub.__name__] = stub
        scene = Scene()
        original.configure_original_source_revision(scene, revision_id='first_remake', reason='New generated source')
        scene['state'] = {'stage': 'QR_READY'}
        with self.assertRaises(ValueError):
            original.configure_original_source_revision(scene, revision_id='second_remake', reason='Changed')
        self.assertEqual(scene['st_original_source_revision'], 'first_remake')
        original.configure_original_source_revision(scene, revision_id='first_remake', reason='Resume')

if __name__ == '__main__': unittest.main()

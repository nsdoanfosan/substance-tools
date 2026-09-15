import importlib.util
from pathlib import Path
import tempfile
import unittest
import sys
import types

package = types.ModuleType('_source_revision_policy_test')
package.__path__ = [str(Path(__file__).parents[1])]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + '.source_revision', Path(__file__).parents[1] / 'source_revision.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class RevisionPolicyTests(unittest.TestCase):
    def test_legacy_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / '00_source_original_once'
            self.assertEqual(policy.active_archive_parent({}, source), Path(folder).resolve())

    def test_revision_keeps_original_archive_and_independent_location(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / '00_source_original_once'
            source.mkdir()
            (source / 'proof').write_text('unchanged')
            result = policy.active_archive_parent({'source_map_revision_id': 'projection_02'}, source)
            self.assertEqual(result, Path(folder).resolve() / 'source_map_revisions' / 'projection_02')
            self.assertFalse(result.exists())
            self.assertEqual((source / 'proof').read_text(), 'unchanged')

    def test_unsafe_or_wrong_roots_are_rejected(self):
        for token in ('../outside', '', 'short', '/absolute', 'a' * 81):
            with self.assertRaises(ValueError):
                policy.revision_parent('00_source_original_once', token)
        with self.assertRaises(ValueError):
            policy.revision_parent('other_archive', 'projection_02')


if __name__ == '__main__':
    unittest.main()

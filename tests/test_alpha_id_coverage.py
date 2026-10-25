"""Transparent alpha padding must not select unrelated fabric in an ID mask."""
import ast
import unittest
from pathlib import Path


def load_mask_copy():
    source = Path(__file__).resolve().parents[1] / 'core.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_copy_mask_to_alpha')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_copy_mask_to_alpha']


class Image:
    def __init__(self, pixels):
        self.pixels = list(pixels)

    def update(self):
        pass


class AlphaIDCoverageTests(unittest.TestCase):
    def test_resolution_mismatch_cannot_silently_shift_coverage(self):
        image = Image([0, 1, 1, 1])
        coverage = Image([1, 1, 1, 1] * 4)
        with self.assertRaisesRegex(ValueError, 'identical pixel counts'):
            load_mask_copy()(image, coverage, clear_uncovered_rgb=True)

    def test_id_padding_cannot_select_fabric_outside_the_source(self):
        image = Image([0, 1, 1, 1, 0, 0, 1, 1])
        coverage = Image([0, 0, 0, 1, 1, 1, 1, 1])
        load_mask_copy()(image, coverage, clear_uncovered_rgb=True)
        self.assertEqual(image.pixels, [0, 0, 0, 0, 0, 0, 1, 1])

    def test_color_and_normal_workflow_keeps_its_transparent_padding(self):
        image = Image([0.5, 0.5, 1, 1])
        coverage = Image([0, 0, 0, 1])
        load_mask_copy()(image, coverage)
        self.assertEqual(image.pixels, [0.5, 0.5, 1, 0])

    def test_partial_coverage_does_not_add_a_different_id(self):
        image = Image([0, 1, 1, 1, 0, 1, 1, 1])
        coverage = Image([0.2, 0.2, 0.2, 1, 0.8, 0.8, 0.8, 1])
        load_mask_copy()(image, coverage, clear_uncovered_rgb=True)
        self.assertEqual(image.pixels, [0, 0, 0, 0.2, 0, 1, 1, 0.8])


if __name__ == '__main__':
    unittest.main()

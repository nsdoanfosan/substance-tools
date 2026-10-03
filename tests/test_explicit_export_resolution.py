import copy
import unittest
from unittest import mock

from test_painter_pending_create import _load_plugin_with_import_stubs


class ExplicitExportResolutionTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.painter = _load_plugin_with_import_stubs()
        self.painter.export.export_project_textures = mock.Mock(return_value='exported')
        self.request = {'texture_dir': 'output', 'preset': 'Unreal_V2', 'export_resolution': 4096}

    def test_1k_working_project_exports_explicit_4k_without_project_mutation(self):
        self.painter.textureset.set_resolutions = mock.Mock()
        before = copy.deepcopy(self.request)
        self.assertEqual(self.plugin._export_textures_with_preset(self.request, [{'rootPath': 'metal'}], 'Unreal_V2'), 'exported')
        config = self.painter.export.export_project_textures.call_args.args[0]
        self.assertEqual(config['exportParameters'][0]['parameters']['sizeLog2'], 12)
        self.assertEqual(self.request, before)
        self.painter.textureset.set_resolutions.assert_not_called()

    def test_inline_preset_and_existing_requests_keep_their_contract(self):
        self.request.pop('export_resolution')
        self.plugin._export_textures_with_preset(self.request, [], {'name': 'kept', 'maps': []})
        config = self.painter.export.export_project_textures.call_args.args[0]
        self.assertNotIn('sizeLog2', config['exportParameters'][0]['parameters'])
        self.assertEqual(config['exportPresets'][0]['name'], 'kept')

    def test_invalid_size_fails_before_native_export(self):
        for value in (0, -1, True, 4000, '4096', 32768):
            with self.subTest(value=value):
                self.request['export_resolution'] = value
                with self.assertRaises(ValueError):
                    self.plugin._export_textures_with_preset(self.request, [], 'Unreal_V2')
        self.painter.export.export_project_textures.assert_not_called()


if __name__ == '__main__':
    unittest.main()

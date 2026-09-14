import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

path=Path(__file__).parents[1]/'painter/startup/substance_tools_unreal_viewport/existing_project.py'
spec=importlib.util.spec_from_file_location('existing_project_test_module',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ExistingProjectTests(unittest.TestCase):
    def test_preserve_then_close_then_open(self):
        with tempfile.TemporaryDirectory() as raw:
            current=Path(raw)/'current.spp';current.write_bytes(b'original')
            target=Path(raw)/'target.spp';target.write_bytes(b'target')
            project=Mock();project.is_busy.return_value=False;project.is_open.return_value=True
            project.is_in_edition_state.return_value=True;project.needs_saving.return_value=True
            project.file_path.return_value=str(current)
            request={'action':'UPDATE','open_existing_project':True,'spp':str(target)}
            module.handle_existing_project(project,request,lambda x:None)
            project.save.assert_not_called();project.close.assert_not_called()
            request['preserve_open_project']=True
            module.handle_existing_project(project,request,lambda x:None)
            project.save.assert_called_once();project.close.assert_not_called()
            project.needs_saving.return_value=False
            module.handle_existing_project(project,request,lambda x:None)
            project.close.assert_called_once();project.open.assert_not_called()
            project.is_open.return_value=False
            module.handle_existing_project(project,request,lambda x:None)
            project.open.assert_called_once_with(str(target))
    def test_busy_and_matching_target_are_untouched(self):
        with tempfile.TemporaryDirectory() as raw:
            target=Path(raw)/'target.spp';target.write_bytes(b'target')
            project=Mock();project.is_busy.return_value=True
            request={'action':'UPDATE','open_existing_project':True,'spp':str(target)}
            module.handle_existing_project(project,request,lambda x:None)
            project.close.assert_not_called()
            project.is_busy.return_value=False;project.is_open.return_value=True
            project.file_path.return_value=str(target)
            module.handle_existing_project(project,request,lambda x:None)
            project.close.assert_not_called();project.save.assert_not_called()
    def test_reject_missing_target_and_ignore_ordinary_request(self):
        project=Mock()
        self.assertFalse(module.handle_existing_project(project,{'action':'CREATE'},lambda x:None))
        with self.assertRaises(ValueError):
            module.handle_existing_project(project,{'action':'UPDATE','open_existing_project':True,'spp':'missing.spp'},lambda x:None)

if __name__=='__main__':unittest.main()

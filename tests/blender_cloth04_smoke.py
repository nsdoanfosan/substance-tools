"""Disposable factory-scene checks for the exact checkout; never save preferences."""
import runpy
import sys
import tempfile
import types
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
package=types.ModuleType('substance_tools')
package.__path__=[str(repo)]
sys.modules['substance_tools']=package
test=sys.argv[sys.argv.index('--')+1]
with tempfile.TemporaryDirectory(prefix='cloth04_fbx_qa_') as folder:
    # Existing smoke's one mkdtemp call is bounded to this context for cleanup.
    original=tempfile.mkdtemp
    tempfile.mkdtemp=lambda *args,**kwargs:folder
    try:
        runpy.run_path(str(repo/'tests'/test),run_name='__main__')
    finally:
        tempfile.mkdtemp=original

"""Load the shared stdlib helper without importing Painter's startup package."""
import importlib.util
from pathlib import Path

_source = Path(__file__).parent / 'painter/startup/substance_tools_unreal_viewport/workstation_coordination.py'
_spec = importlib.util.spec_from_file_location('_substance_tools_native_coordination', _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError('Substance Tools workstation coordination helper is missing')
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)

require_phase = _helper.require_phase
preflight_pending = _helper.preflight_pending
publish_pending = _helper.publish_pending
publish_request_copies = _helper.publish_request_copies
write_receipt = _helper.write_receipt
discard_pending = _helper.discard_pending
attach_phase = _helper.attach_phase
can_execute = _helper.can_execute
heartbeat = _helper.heartbeat
complete = _helper.complete
fail = _helper.fail
start_followup = _helper.start_followup
APPLY_PIPELINE = _helper.APPLY_PIPELINE


def _apply_journal():
    from .blender_apply_completion import journal
    return journal(_helper)


def resume_apply(parent_phase_id, phase_id, filepath):
    return _apply_journal().resume(parent_phase_id, phase_id, filepath)


def begin_apply(request, scene, export_result):
    return _apply_journal().begin(request, scene, export_result)


def applied_awaiting_save(request, scene, receipt):
    return _apply_journal().applied(request, scene, receipt)


def register_apply_receipts():
    from .blender_apply_completion import register
    register(_helper)


def unregister_apply_receipts():
    from .blender_apply_completion import unregister
    unregister()

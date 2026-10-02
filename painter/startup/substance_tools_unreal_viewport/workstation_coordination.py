"""Admission for existing Substance Tools requests; never run a second pipeline."""
from __future__ import annotations

import importlib.util
import json
import ntpath
import os
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path


PIPELINE = 'substance-tools'
APPLY_PIPELINE = 'substance-tools.blender-apply'
RESOURCE = 'painter'
HEARTBEAT_SECONDS = 45.0
_bridge_lock = threading.Lock()
_bridge_module = None
_bridge_revision = None
_heartbeat_attempts = {}
_FIELDS = ('phase_id', 'provider', 'session_id', 'resource', 'pipeline', 'request_id', 'target', 'ticket_id')


def _bridge():
    global _bridge_module, _bridge_revision
    root = Path(os.environ.get('WORKSTATION_QUEUE_REPO') or Path.home() / 'Documents/GitHub/workstation-queue')
    source = (root / 'pipeline_bridge.py').resolve()
    stat = source.stat()
    revision = (str(source), stat.st_mtime_ns, stat.st_size)
    with _bridge_lock:
        if _bridge_revision != revision:
            for name in ('queue_store', 'work_phases', 'wq_paths', 'machine_capacity'):
                cached = sys.modules.get(name)
                if cached is not None and (not getattr(cached, '__file__', None)
                        or Path(cached.__file__).resolve() != source.parent / f'{name}.py'):
                    raise RuntimeError(f'Workstation queue module collision: {name}')
            spec = importlib.util.spec_from_file_location('_substance_tools_workstation_bridge', source)
            if spec is None or spec.loader is None:
                raise RuntimeError('Workstation queue pipeline bridge could not be loaded')
            module = importlib.util.module_from_spec(spec)
            # Source execution bypasses same-second/same-size bytecode caches.
            entry = str(source.parent)
            sys.path.insert(0, entry)
            try:
                exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
            finally:
                sys.path.remove(entry)
            _bridge_module = module
            _bridge_revision = revision
        return _bridge_module


def require_phase(phase_id, *, resource=RESOURCE):
    if not phase_id:
        return None  # Existing manual buttons retain their original behavior.
    bridge = _bridge()
    phase = bridge.require_active_phase(phase_id, resource)
    bridge.require_scopes(phase_id, ['editor'])
    return phase


def _path(value):
    return ntpath.normpath(str(value)).replace('\\', '/').casefold()


def _validate_apply_resource(resource, target):
    family, separator, declared_path = str(resource).partition(':')
    if family.casefold() != 'blender':
        raise ValueError('The apply phase must declare its Blender resource')
    if separator and (not target or _path(declared_path) != _path(target)):
        raise ValueError('The apply phase belongs to a different original Blender file')


def preflight_pending(path, *, phase_id='', target='', request_id=''):
    """Keep the one native handoff slot; repeated same-phase work is a no-op."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        existing = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise RuntimeError('Existing Painter handoff cannot be verified; it was preserved') from error
    if not isinstance(existing, dict):
        raise RuntimeError('Existing Painter handoff is invalid; it was preserved')
    if existing.get('status') in {'SUCCESS', 'FAILED'}:
        return None
    marker = existing.get('request_id') or existing.get('pipeline_hash')
    metadata = existing.get('workstation_phase') or {}
    same_phase = (phase_id and isinstance(metadata, dict) and metadata.get('phase_id') == phase_id
                  and metadata.get('request_id') == marker and target and _path(existing.get('spp', '')) == _path(target))
    if (request_id and marker == request_id) or same_phase:
        return existing
    raise RuntimeError('Another Painter handoff is still pending; its native request was preserved')


@contextmanager
def _publication_lock(path):
    """Short OS-owned lock: process exits release it, including crashes."""
    lock_path = Path(path).with_name(Path(path).name + '.publish.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b', buffering=0) as handle:
        try:
            # Initialization can race another process taking the first-byte
            # lock. Keep writes unbuffered and treat that contention as a wait.
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'\0')
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Another native handoff is being published; retry after it finishes') from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _replace_json(path, payload, previous, *, allow_removed=False):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.publishing')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        # A consumer may remove the previous terminal receipt; replacing its
        # absence is safe. A changed record belongs to another publisher.
        current = path.read_bytes() if path.exists() else None
        if current != previous and (current is not None or not allow_removed):
            raise RuntimeError('Native handoff changed during publication; the newer request was preserved')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _publish_locked(path, request):
    path = Path(path)
    marker = request.get('request_id') or request.get('pipeline_hash')
    if not marker:
        raise ValueError('An exact native request ID is required for publication')
    previous = path.read_bytes() if path.exists() else None
    if previous is not None:
        try:
            recorded = json.loads(previous.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            recorded = None
        if isinstance(recorded, dict) and (recorded.get('request_id') or recorded.get('pipeline_hash')) == marker:
            return False  # Preserve an existing SUCCESS/FAILED receipt too.
    existing = preflight_pending(path, request_id=marker)
    if existing is not None:
        return False  # An exact repeated native dispatch keeps its receipt.
    return _replace_json(path, request, previous, allow_removed=True)


def write_receipt(path, request, receipt):
    """An old native callback can update only its own still-matching file."""
    path = Path(path)
    with _publication_lock(path):
        if not path.exists():
            return False
        previous = path.read_bytes()
        try:
            existing = json.loads(previous.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            return False
        marker = request.get('request_id') or request.get('pipeline_hash')
        if not isinstance(existing, dict) or not marker or (existing.get('request_id') or existing.get('pipeline_hash')) != marker:
            return False
        if existing.get('workstation_phase') != request.get('workstation_phase'):
            return False
        if (receipt.get('request_id') or receipt.get('pipeline_hash')) != marker:
            raise ValueError('Native receipt differs from the accepted request ID')
        return _replace_json(path, receipt, previous)


def publish_pending(path, request):
    """Atomically publish to the native single slot without replacing live work."""
    with _publication_lock(path):
        return _publish_locked(path, request)


def publish_request_copies(paths, request):
    """Validate all native destinations before writing either request copy."""
    paths = sorted({Path(path).resolve() for path in paths}, key=lambda path: str(path).casefold())
    marker = request.get('request_id') or request.get('pipeline_hash')
    if not marker:
        raise ValueError('An exact native request ID is required for publication')
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(_publication_lock(path))
        for path in paths:
            preflight_pending(path, request_id=marker)
        return [_publish_locked(path, request) for path in paths]


def discard_pending(path, request):
    """Cancel only this producer's exact native file, never a newer request."""
    path = Path(path)
    with _publication_lock(path):
        if not path.exists():
            return False
        try:
            existing = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return False
        marker = request.get('request_id') or request.get('pipeline_hash')
        if not isinstance(existing, dict) or not marker or (existing.get('request_id') or existing.get('pipeline_hash')) != marker:
            return False
        path.unlink()
        return True


def attach_phase(request, phase_id, *, pipeline=PIPELINE, target=None, resource=RESOURCE):
    if not phase_id:
        return request
    marker = request.get('request_id') or request.get('pipeline_hash')
    target = target or request.get('spp')
    if not marker or not target:
        raise ValueError('A native Painter request ID and SPP target are required')
    if pipeline == APPLY_PIPELINE:
        _validate_apply_resource(resource, target)
    require_phase(phase_id, resource=resource)
    metadata = _bridge().bind_handoff(phase_id, pipeline, marker, target)
    if (not isinstance(metadata, dict) or any(not metadata.get(key) for key in _FIELDS)
            or any('token' in str(key).casefold() for key in metadata)):
        raise RuntimeError('Workstation queue returned an invalid native handoff binding')
    # Only the public binding crosses JSON IPC. Tokens never enter request files.
    request['workstation_phase'] = dict(metadata)
    if pipeline == APPLY_PIPELINE:
        request['workstation_target'] = str(target)
    return request


def _metadata(request):
    if 'workstation_phase' not in request:
        return None
    metadata = request.get('workstation_phase')
    marker = request.get('request_id') or request.get('pipeline_hash')
    if (not isinstance(metadata, dict) or any(not metadata.get(key) for key in _FIELDS)
            or metadata['request_id'] != marker or metadata['pipeline'] not in {PIPELINE, APPLY_PIPELINE}
            or _path(metadata['target']) != _path(request.get('workstation_target') or request.get('spp', ''))):
        raise ValueError('Painter request differs from its workstation phase binding')
    return metadata


def can_execute(request):
    try:
        metadata = _metadata(request)
        return metadata is None or bool(_bridge().can_execute_handoff(metadata))
    except Exception:
        return False  # Admission unavailable: keep the native request unchanged.


def start_followup(request, phase_id, *, target=None):
    metadata = _metadata(request)
    if metadata is None or metadata['pipeline'] != PIPELINE:
        raise ValueError('A completed parent Painter handoff is required')
    bridge = _bridge()
    phase = next((item for item in bridge.Store(bridge.db_path()).phases_snapshot() if item['id'] == phase_id), None)
    if phase is None:
        raise ValueError('The requested Blender apply phase does not exist')
    _validate_apply_resource(phase['resource'], target)
    return bridge.start_followup_phase(metadata, phase_id)


def heartbeat(request):
    """At most one database-only lease heartbeat per handoff every 45 seconds."""
    try:
        metadata = _metadata(request)
        if metadata is None:
            return True
        key = (metadata['phase_id'], metadata['request_id'])
        now = time.monotonic()
        previous = _heartbeat_attempts.get(key)
        if previous is not None and now - previous < HEARTBEAT_SECONDS:
            return True
        _heartbeat_attempts[key] = now
        return bool(_bridge().heartbeat_handoff(metadata))
    except Exception:
        return False


def complete(request):
    try:
        metadata = _metadata(request)
        if metadata is None:
            return True
        if metadata['pipeline'] == APPLY_PIPELINE:
            # In-memory apply is not a saved Blend. Only the Blender journal's
            # paired native save callbacks may deliver this saved receipt.
            return False
        completed = bool(_bridge().complete_handoff(metadata, metadata['request_id'], metadata['target']))
        if completed:
            _heartbeat_attempts.pop((metadata['phase_id'], metadata['request_id']), None)
        return completed
    except Exception:
        return False


def fail(request, note):
    try:
        metadata = _metadata(request)
        if metadata is None:
            return True
        failed = bool(_bridge().fail_handoff(metadata, str(note)))
        if failed:
            _heartbeat_attempts.pop((metadata['phase_id'], metadata['request_id']), None)
        return failed
    except Exception:
        return False

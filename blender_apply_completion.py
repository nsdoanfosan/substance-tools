"""Durable coordinated apply receipts. Never invokes a Blender save or apply.

Only a paired native save_pre/save_post event can produce saved evidence. The
scene marker identifies the applied request; the disk journal survives a crash
before the Blend containing that marker has been saved. Uncertain application
is deliberately not replayed. Original phase goals/checkpoints stay in queue.
"""
import hashlib
import json
import time
import uuid
from pathlib import Path


MARKER = '_substance_tools_applied_requests'


class ApplyJournal:
    def __init__(self, coordination, bpy, root=None):
        self.coordination = coordination
        self.bpy = bpy
        self.root = Path(root or Path.home() / '.substance-tools' / 'workstation-apply')
        self.armed = {}

    def _path(self, phase_id):
        return self.root / (hashlib.sha256(phase_id.encode('utf-8')).hexdigest() + '.json')

    def _read(self, path):
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        metadata = self.coordination._metadata(record['request'])
        if (record.get('version') != 1 or metadata is None
                or metadata['pipeline'] != self.coordination.APPLY_PIPELINE
                or self._path(metadata['phase_id']) != path):
            raise ValueError('Invalid Blender apply journal; preserve it for owner recovery')
        return record

    def _write(self, path, record):
        previous = path.read_bytes() if path.exists() else None
        return self.coordination._replace_json(path, record, previous)

    def _matches_file(self, record, filepath):
        return bool(filepath) and self.coordination._path(filepath) == self.coordination._path(
            record['request']['workstation_phase']['target'])

    def _markers(self, scene):
        return json.loads(scene.get(MARKER, '{}'))

    def _has_marker(self, record):
        return any(self._markers(scene).get(record['request']['workstation_phase']['phase_id'])
                   == record['nonce'] for scene in self.bpy.data.scenes)

    @staticmethod
    def _fingerprint(filepath):
        stat = Path(filepath).stat()
        if stat.st_size <= 0:
            raise ValueError('Saved Blend is empty')
        return dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)

    def resume(self, parent_phase_id, phase_id, filepath):
        """Check before dispatch, including after restart; never repeat native work."""
        path = self._path(phase_id)
        record = self._read(path)
        if record is None:
            return None
        if record['parent_phase_id'] != parent_phase_id or not self._matches_file(record, filepath):
            raise ValueError('Existing apply belongs to another parent request or Blend; preserved')
        self.reconcile(path)
        return self._read(path)['state']

    def begin(self, request, scene, export_result):
        metadata = self.coordination._metadata(request)
        if metadata is None:
            return True
        if (metadata['pipeline'] != self.coordination.APPLY_PIPELINE
                or not self.coordination.can_execute(request)):
            raise ValueError('Blender apply no longer owns its exact execution')
        path = self._path(metadata['phase_id'])
        with self.coordination._publication_lock(path):
            existing = self._read(path)
            if existing is not None:
                if existing['request']['workstation_phase'] != metadata:
                    raise ValueError('Existing apply belongs to another execution; preserved')
                return False
            if export_result.get('request_id') != metadata['request_id']:
                raise ValueError('Apply export receipt differs from native request')
            record = dict(version=1, state='applying', nonce=uuid.uuid4().hex,
                          request=request, parent_phase_id=request['workstation_parent_phase_id'],
                          export_result=export_result, applied_receipt=None,
                          dirty_before_apply=bool(self.bpy.data.is_dirty), created_at=time.time())
            if not self._matches_file(record, self.bpy.data.filepath):
                raise ValueError('Apply target differs from current Blend')
            # Written before the first native mutation. A crash may leave an
            # uncertain apply; neither a retry nor a reload can silently replay it.
            self._write(path, record)
        return True

    def applied(self, request, scene, receipt):
        metadata = self.coordination._metadata(request)
        if metadata is None:
            return
        path = self._path(metadata['phase_id'])
        with self.coordination._publication_lock(path):
            record = self._read(path)
            if (record is None or record['state'] != 'applying'
                    or record['request']['workstation_phase'] != metadata
                    or not self._matches_file(record, self.bpy.data.filepath)
                    or not self.coordination.can_execute(request)):
                raise ValueError('Apply completion lost its exact execution; owner recovery required')
            markers = self._markers(scene)
            markers[metadata['phase_id']] = record['nonce']
            scene[MARKER] = json.dumps(markers, sort_keys=True)
            record.update(state='applied_awaiting_save', applied_receipt=receipt)
            self._write(path, record)

    def _current_records(self):
        for path in self.root.glob('*.json'):
            record = self._read(path)
            if (record['state'] in {'applied_awaiting_save', 'saved'}
                    and self._matches_file(record, self.bpy.data.filepath) and self._has_marker(record)):
                yield path, record

    def save_pre(self, filepath):
        self.armed.clear()
        for path, record in self._current_records():
            if self._matches_file(record, filepath):
                self.armed[str(path)] = dict(record=record, before=self._fingerprint(filepath))

    def save_failed(self, filepath):
        armed, self.armed = self.armed, {}
        for entry in armed.values():
            record = entry['record']
            if self._matches_file(record, filepath):
                self.coordination.fail(record['request'],
                    'Blend save failed/cancelled; applied result retained, do not re-export or reapply')

    def save_post(self, filepath):
        armed, self.armed = self.armed, {}
        for name, entry in armed.items():
            path, observed = Path(name), entry['record']
            with self.coordination._publication_lock(path):
                record = self._read(path)
                if (record != observed or not self._matches_file(record, filepath)
                        or not self._matches_file(record, self.bpy.data.filepath)
                        or not self._has_marker(record)):
                    continue
                fingerprint = self._fingerprint(filepath)
                if fingerprint == entry['before']:
                    continue  # A callback alone is not a changed persisted file.
                record.update(state='saved', saved=dict(filepath=str(filepath),
                              fingerprint=fingerprint, saved_at=time.time()))
                self._write(path, record)
            self.reconcile(path)

    def reconcile(self, path):
        """Retry delivery of verified evidence only, never export/apply/save."""
        with self.coordination._publication_lock(path):
            record = self._read(path)
            if record is None or record['state'] != 'saved':
                return False
            saved = record['saved']
            if (not self._matches_file(record, saved['filepath'])
                    or self._fingerprint(saved['filepath']) != saved['fingerprint']):
                return False
            metadata = self.coordination._metadata(record['request'])
            # The bridge verifies the complete binding, including ticket ID;
            # an old save cannot complete a later recovered owner execution.
            if not self.coordination._bridge().complete_handoff(
                    metadata, metadata['request_id'], metadata['target']):
                return False
            record['state'] = 'completed'
            self._write(path, record)
            return True

    def tick(self):
        for path, record in self._current_records():
            if record['state'] == 'saved':
                self.reconcile(path)
            else:
                self.coordination.heartbeat(record['request'])


_journal = None
_registered = []


def journal(coordination):
    global _journal
    if _journal is None:
        import bpy
        _journal = ApplyJournal(coordination, bpy)
    return _journal


def _guarded(action, *args):
    try:
        action(*args)
    except Exception as error:
        # Retain journal/reservation on disk/queue failure. Do not clear evidence.
        print('Substance Tools apply receipt requires owner recovery:', error)


def native_apply_save_pre(filepath):
    _guarded(_journal.save_pre, filepath)


def native_apply_save_post(filepath):
    _guarded(_journal.save_post, filepath)


def native_apply_save_failed(filepath):
    _guarded(_journal.save_failed, filepath)


def native_apply_load_pre(_filepath):
    _journal.armed.clear()


def native_apply_tick():
    _guarded(_journal.tick)
    return 45.0


def register(coordination):
    import bpy
    journal(coordination)
    for name, handler in (('save_pre', native_apply_save_pre),
                          ('save_post', native_apply_save_post),
                          ('save_post_fail', native_apply_save_failed),
                          ('load_pre', native_apply_load_pre)):
        handlers = getattr(bpy.app.handlers, name)
        for old in list(handlers):
            if (getattr(old, '__module__', '') == __name__
                    and getattr(old, '__name__', '') == handler.__name__):
                handlers.remove(old)
        handlers.append(bpy.app.handlers.persistent(handler))
        _registered.append((handlers, handler))
    if not bpy.app.timers.is_registered(native_apply_tick):
        bpy.app.timers.register(native_apply_tick, first_interval=45.0, persistent=True)


def unregister():
    import bpy
    for handlers, handler in _registered:
        if handler in handlers:
            handlers.remove(handler)
    _registered.clear()
    if bpy.app.timers.is_registered(native_apply_tick):
        bpy.app.timers.unregister(native_apply_tick)
    if _journal:
        _journal.armed.clear()

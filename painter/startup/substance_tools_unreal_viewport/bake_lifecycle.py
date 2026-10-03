"""Observe one SDK bake. Timeouts diagnose uncertainty; they never replay work."""
import time


class BakeLifecycle:
    START_TIMEOUT = 30.0
    PROGRESS_TIMEOUT = 600.0
    END_TIMEOUT = 90.0
    SAVE_TIMEOUT = 120.0

    def __init__(self, *, current, changed, ended, clock=time.perf_counter):
        self.current = current
        self.changed = changed
        self.on_end = ended
        self.clock = clock
        self.stop_source = None
        self.pending = []
        self.dispatching = True
        self.closed = False
        self.state = 'dispatching'
        self.progress_value = None
        self.dispatched_at = clock()
        self.last_event_at = self.dispatched_at
        self.end_status = None
        self.diagnostic = None
        self.recovery_required = False
        self._last_emission = None

    def valid(self):
        return not self.closed and self.current()

    def snapshot(self):
        return {'state': self.state, 'progress': self.progress_value,
                'end_status': self.end_status, 'diagnostic': self.diagnostic,
                'recovery_required': self.recovery_required,
                'seconds_since_dispatch': max(0.0, self.clock() - self.dispatched_at),
                'seconds_since_event': max(0.0, self.clock() - self.last_event_at),
                'automatic_replay': False}

    def emit(self):
        if self.valid():
            key = (self.state, int((self.progress_value or 0.0) * 10), self.diagnostic)
            # Persist milestones/10% progress, not a disk transaction per SDK tick.
            if key != self._last_emission:
                self._last_emission = key
                self.changed(self.snapshot())

    def bind(self, stop_source):
        if not self.valid():
            self.close()
            return
        self.stop_source = stop_source
        self.dispatching = False
        self.state = 'dispatched'
        self.emit()
        pending, self.pending = self.pending, []
        for kind, event in pending:
            getattr(self, kind)(event)

    def accept(self, kind, event):
        if not self.valid():
            return False
        if self.dispatching:
            self.pending.append((kind, event))
            return False
        return True

    def started(self, event):
        if not self.accept('started', event):
            return
        # AboutToStart is the only SDK bake event carrying the job identity.
        # Retaining this handle is ownership, not a claim about GC causing failure.
        if event.stop_source != self.stop_source:
            if self.state in ('started', 'progress'):
                # Progress/end have no SDK job token. Another start makes those
                # subsequent global events ambiguous, so they cannot permit save.
                self.state = 'job_conflict'
                self.uncertain('another_native_bake_started')
            return
        if self.state != 'dispatched':
            return
        self.state = 'started'
        self.last_event_at = self.clock()
        self.diagnostic = None
        self.recovery_required = False
        self.emit()

    def progress(self, event):
        if not self.accept('progress', event) or self.state not in ('started', 'progress'):
            return
        value = float(event.progress)
        if not 0.0 <= value <= 1.0:
            return
        self.state = 'progress'
        if value != self.progress_value:
            self.last_event_at = self.clock()
            self.diagnostic = None
            self.recovery_required = False
        self.progress_value = value
        self.emit()

    def ended(self, event):
        if not self.accept('ended', event):
            return
        if self.state not in ('started', 'progress'):
            return  # An unmatched/global or duplicate end cannot authorize saving.
        self.state = 'ended'
        self.end_status = str(event.status)
        self.last_event_at = self.clock()
        self.diagnostic = None
        self.recovery_required = False
        self.emit()
        self.on_end(event)

    def saving(self):
        if not self.valid() or self.state != 'ended':
            return False
        self.state = 'save_pending'
        self.last_event_at = self.clock()
        self.emit()
        return True

    def saved(self):
        if not self.valid() or self.state != 'save_pending':
            return False
        self.state = 'saved'
        self.last_event_at = self.clock()
        self.emit()
        return True

    def uncertain(self, reason):
        if self.valid():
            self.diagnostic = reason
            self.recovery_required = True
            self.emit()

    def check_timeout(self):
        if not self.valid():
            return self.snapshot()
        age = self.clock() - self.last_event_at
        problem = None
        if self.state == 'dispatched' and age >= self.START_TIMEOUT:
            problem = 'native_start_unacknowledged'
        elif self.state in ('started', 'progress'):
            if self.progress_value == 1.0 and age >= self.END_TIMEOUT:
                problem = 'native_end_unacknowledged'
            elif age >= self.PROGRESS_TIMEOUT:
                problem = 'native_progress_stalled'
        elif self.state == 'save_pending' and age >= self.SAVE_TIMEOUT:
            problem = 'native_save_unacknowledged'
        if problem and self.diagnostic != problem:
            self.diagnostic = problem
            self.recovery_required = True
            self.emit()
        return self.snapshot()

    def close(self):
        self.closed = True
        self.pending.clear()
        self.stop_source = None

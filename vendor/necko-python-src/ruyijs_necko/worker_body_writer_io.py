"""One actual body writer; all leases remain exclusively dispatcher-owned."""
import threading
from .worker_body_outbox import BodyOutbox
from .worker_body_types import BodyFault, HandoffStopped
from . import worker_body_write_handoff as handoff


def _frame(entry):
    # Only the current operation creates payload/frame copies, never the queue.
    payload = entry.ticket.lease.read()
    if len(payload) != entry.prepared.header.length:
        raise ValueError("Body writer lease length changed")
    return entry.prepared.prefix + payload


class BodyWriterIO:
    def __init__(self, channel, wake, stall_ms=30000):
        self.channel = channel
        self.outbox = BodyOutbox(wake, stall_ms)
        self._start_lock = threading.Lock()
        self._started = False
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name='ruyijs-body-writer')
        self._emergency_fault = BodyFault('body-write-io', 'ResourceError', 0)

    def start(self):
        with self._start_lock:
            if self._started:
                raise ValueError("Body writer already started")
            self._started = True
            try:
                self._thread.start()
            except BaseException:
                self.outbox.fail(self._emergency_fault)
                self.channel.stop()
                self._finished.set()
                raise

    def stop(self):
        self.outbox.stop()
        self.channel.stop()

    def join(self, timeout=None):
        with self._start_lock:
            started = self._started
            launched = self._thread.ident is not None
        if not started:
            raise ValueError("Body writer has not started")
        if launched:
            self._thread.join(timeout)
            return not self._thread.is_alive() and self._finished.is_set()
        return self._finished.is_set()

    def confirm(self, completion):
        handoff.confirm(self.outbox, completion)

    def _run(self):
        phase = 'body-write-take'
        try:
            while True:
                phase = 'body-write-take'
                entry = handoff.take(self.outbox)
                phase = 'body-write-encode'
                frame = _frame(entry)
                phase = 'body-write-payload'
                self.channel.write_exact(frame, entry.deadline)
                del frame
                handoff.complete(self.outbox, entry)
                phase = 'body-write-consume'
                handoff.wait_confirmation(self.outbox, entry)
                del entry
        except HandoffStopped:
            pass
        except BaseException as error:
            try:
                code = getattr(error, 'winerror', 0)
                code = code if type(code) is int and 0 <= code <= 0xffffffff else 0
                kind = 'DeadlineExceeded' if isinstance(error, TimeoutError) or code == 258 else \
                    'Cancelled' if code == 995 else 'ResourceError' if isinstance(error, MemoryError) else 'ProtocolError'
                self.outbox.fail(BodyFault(phase, kind, code))
            except BaseException:
                self.outbox.fail(self._emergency_fault)
        finally:
            self.channel.stop()
            self._finished.set()

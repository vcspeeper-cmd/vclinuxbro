"""Stage one real body pipe read; dispatcher alone owns ledgers and lease refunds."""
import threading
from .worker_body_handoff import BodyHandoff
from .worker_body_types import BodyHeader, HeaderRequest, BodyCompletion, BodyFault, HandoffStopped


class BodyReaderIO:
    def __init__(self, channel, wake, stall_ms=30000):
        if type(stall_ms) is not int or not 1 <= stall_ms <= 60000:
            raise ValueError("Body frame stall budget must be 1..60000ms")
        self.channel = channel
        self.handoff = BodyHandoff(wake)
        self._stall = stall_ms
        self._start_lock = threading.Lock()
        self._started = False
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name='ruyijs-body-reader')
        self._emergency_fault = BodyFault('body-io', 'ResourceError', 0)

    def start(self):
        with self._start_lock:
            if self._started:
                raise ValueError("Body reader already started")
            self._started = True
            try:
                self._thread.start()
            except BaseException:
                self.handoff.fail(self._emergency_fault)
                self.channel.stop()
                self._finished.set()
                raise

    def stop(self):
        self.handoff.stop()
        self.channel.stop()

    def join(self, timeout=None):
        with self._start_lock:
            started = self._started
            launched = self._thread.ident is not None
        if not started:
            raise ValueError("Body reader has not started")
        if launched:
            self._thread.join(timeout)
            return not self._thread.is_alive() and self._finished.is_set()
        return self._finished.is_set()

    def _run(self):
        phase = 'body-prefix'
        try:
            while True:
                phase = 'body-prefix'
                prefix, deadline = self.channel.read_start(52, self._stall)
                phase = 'body-header'
                request = HeaderRequest(BodyHeader.decode(prefix), deadline)
                self.handoff.publish_request(request)
                phase = 'body-permit'
                lease = self.handoff.wait_permit(request)
                completion = BodyCompletion(request, lease)
                phase = 'body-payload'
                data = self.channel.read_exact(request.header.length, request.deadline)
                lease.write(data)
                del data
                self.handoff.publish_completion(completion)
                phase = 'body-consume'
                self.handoff.wait_confirmation(request)
                del completion, lease, request, prefix
        except HandoffStopped:
            pass
        except BaseException as error:
            try:
                code = getattr(error, 'winerror', 0)
                code = code if type(code) is int and 0 <= code <= 0xffffffff else 0
                kind = 'DeadlineExceeded' if isinstance(error, TimeoutError) or code == 258 else \
                    'Cancelled' if code == 995 else 'ResourceError' if isinstance(error, MemoryError) else 'ProtocolError'
                self.handoff.fail(BodyFault(phase, kind, code))
            except BaseException:
                self.handoff.fail(self._emergency_fault)
        finally:
            self.channel.stop()
            self._finished.set()

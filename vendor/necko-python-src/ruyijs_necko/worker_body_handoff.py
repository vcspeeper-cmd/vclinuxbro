"""One reader operation, one permit, one completion and one sticky fault."""
import threading
from .worker_body_types import BodyCompletion, HeaderRequest, HandoffStopped
from .worker_slot_lease import SlotLease
from .worker_channel_win import ticks


class BodyHandoff:
    def __init__(self, wake):
        self._wake = wake
        self._condition = threading.Condition()
        self._request = self._lease = self._completion = self._fault = None
        self._consumed = self._stopped = False

    def snapshot(self):
        """Current facts for diagnostics/cleanup, not an edge-triggered work queue."""
        with self._condition:
            request = self._request
            completion = self._completion
            fault = self._fault
            stopped = self._stopped
        return request, completion, fault, stopped

    def pending_header(self):
        with self._condition:
            return self._request if not self._stopped and self._lease is None else None

    def pending_completion(self):
        with self._condition:
            # Completed bytes must remain observable even after stop/fault.
            return self._completion if not self._consumed else None

    def _current(self, request):
        if self._stopped:
            raise HandoffStopped()
        if request is None or request is not self._request:
            raise ValueError("Body handoff operation is stale")

    def publish_request(self, request):
        with self._condition:
            if self._stopped:
                raise HandoffStopped()
            if self._request is not None:
                raise ValueError("Body handoff already holds an operation")
            self._request = request
        self._wake.set()

    def permit(self, request, lease):
        if type(request) is not HeaderRequest:
            raise ValueError("Body permit requires a current header request")
        if type(lease) is not SlotLease or lease.length != 0:
            raise ValueError("Body permit requires an empty actual wire lease")
        with self._condition:
            now = ticks()
            self._current(request)
            if self._lease is not None or self._completion is not None or now >= request.deadline:
                raise ValueError("Body permit is duplicate or expired")
            self._lease = lease
            self._condition.notify_all()

    def _wait(self, request, consumed):
        with self._condition:
            while True:
                self._current(request)
                if consumed and self._consumed:
                    # Confirmation was admitted before its deadline. A later
                    # reader scheduling delay does not undo that completed fact.
                    return self._lease
                remaining = request.deadline - ticks()
                # Stop or a timely confirmation can run in a GC finalizer at
                # ticks; its notification must not be lost before wait begins.
                self._current(request)
                if consumed and self._consumed:
                    return self._lease
                if remaining <= 0:
                    raise TimeoutError("Body handoff deadline elapsed")
                ready = not consumed and self._lease is not None
                if ready:
                    return self._lease
                # Condition.wait allocates its waiter internally; a GC Stop
                # before that waiter is registered can precede the notify. A
                # bounded predicate heartbeat covers that library-level window.
                self._condition.wait(min(remaining / 1000, .05))

    def wait_permit(self, request):
        return self._wait(request, False)

    def publish_completion(self, completion):
        with self._condition:
            # Stop cancels future work; it must not erase a payload that was
            # already fully written to the current authorized lease.
            if (completion.request is not self._request or self._completion is not None
                    or completion.lease is not self._lease):
                raise ValueError("Body completion differs from its permit")
            self._completion = completion
        self._wake.set()

    def confirm(self, completion):
        if type(completion) is not BodyCompletion:
            raise ValueError("Body confirmation requires a current completion")
        with self._condition:
            now = ticks()
            self._current(completion.request)
            if (completion is not self._completion or self._consumed or
                    now >= completion.request.deadline):
                raise ValueError("Body completion confirmation is stale or expired")
            self._consumed = True
            self._condition.notify_all()

    def wait_confirmation(self, request):
        self._wait(request, True)
        with self._condition:
            self._current(request)
            self._request = self._lease = self._completion = None
            self._consumed = False

    def fail(self, fault):
        with self._condition:
            if self._fault is None:
                self._fault = fault
            self._stopped = True
            self._condition.notify_all()
        self._wake.set()

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._wake.set()

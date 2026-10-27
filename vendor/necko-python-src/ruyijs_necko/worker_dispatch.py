"""Dispatcher turn: one control exchange and one download body frame. No page API."""
import time
from threading import Event, get_ident
from .worker_registry import Registry
from .worker_credit import SenderCredit
from .worker_slots import SlotPool
from .worker_body_writer_io import BodyWriterIO
from .worker_body_types import BodyHeader
from .worker_outbound import OutboundBindings
from .worker_outbound_release import apply_credit, finish, reclaim


class Dispatcher:
    def __init__(self, session):
        self.session = session
        self.registry = Registry(session._epoch)
        self._thread = get_ident()
        self.wake = Event()
        self.writer = BodyWriterIO(session._body, self.wake)
        self.pool = SlotPool(4)
        self.bindings = OutboundBindings(self.pool, self.writer.outbox)
        self.credit = SenderCredit()
        self.writer.start()

    def _check(self):
        if get_ident() != self._thread:
            raise ValueError("Dispatcher requires its own thread")

    def accept_reserve(self, deadline_ms):
        self._check()
        envelope = self.session.receive(deadline_ms)
        if envelope["type"] != "reserve":
            raise ValueError("Dispatcher expected a reserve message")
        payload = envelope["payload"]
        result = self.registry.reserve(int(payload["reservationId"]), payload["partition"],
                                       int(payload["deadlineQpc"]), 0)
        if result["event"] != "accepted":
            raise ValueError("Dispatcher rejected the reservation")
        self.session.send("accepted", dict(reservationId=payload["reservationId"],
                                           state="reserved"), deadline_ms)
        return result

    def send_download(self, payload, deadline_ms):
        self._check()
        if type(payload) is not bytes or not payload:
            raise ValueError("Dispatcher download requires bytes")
        self.credit.update(0, None)
        record = self.bindings.reserve(object(), self.credit)
        record.lease.write(payload)
        header = BodyHeader(2, self.session._epoch, 1, 1, 0, record.seq, len(payload))
        prepared = self.bindings.publish(record, header)
        end = time.monotonic() + 5
        while self.writer.outbox.pending_completion() is not prepared.completion:
            if time.monotonic() >= end:
                raise TimeoutError("Download frame was not written")
            time.sleep(0.01)
        finish(self.bindings, prepared.completion)
        apply_credit(self.bindings, self.credit, 0, 0)
        return header

    def close(self):
        # Stop, prove the writer really joined, then reclaim the leases it can
        # no longer reach. A published binding whose epoch failed keeps its
        # lease, so closing the pool without this step would raise and leave a
        # half-closed dispatcher.
        self.writer.stop()
        joined = self.writer.join(2) if self.writer._started else True
        reclaim(self.bindings, joined)
        self.pool.close()

"""Dispatcher-only reserve/publish for one send direction. No HTTP execution."""
from threading import get_ident
from .worker_outbound_record import Binding
from .worker_slots import SlotPool
from .worker_body_outbox import BodyOutbox
from .worker_credit import SenderCredit


class OutboundBindings:
    def __init__(self, pool, outbox):
        if type(pool) is not SlotPool or type(outbox) is not BodyOutbox:
            raise ValueError("Outbound bindings require the actual pool and outbox")
        self._thread = get_ident()
        self._pool, self._outbox = pool, outbox
        self._slots = [None] * 64
        self._depth = 0

    def _enter(self):
        if get_ident() != self._thread or self._depth:
            raise ValueError("Outbound bindings require their dispatcher")
        self._depth = 1

    def _exit(self):
        self._depth = 0

    def _free(self):
        for index, slot in enumerate(self._slots):
            if slot is None:
                return index
        return None

    def _rollback(self, record):
        if record.seq is not None and not record.published:
            record.ledger.abort(record.seq)
            record.seq = None
        if record.ticket is not None and not record.published:
            self._outbox.abort(record.ticket)
            record.ticket = None
        if record.lease is not None and not record.published:
            record.lease.release()
            record.lease = None
        if record.seq is None and record.ticket is None and record.lease is None:
            self._slots[record.index] = None

    def reserve(self, owner, ledger):
        if type(ledger) is not SenderCredit or owner is None:
            raise ValueError("Outbound reserve requires an owner and sender ledger")
        record = Binding(owner, ledger)
        self._enter()
        try:
            index = self._free()
            if index is None:
                raise ValueError("Outbound binding table is full")
            record.index = index
            self._slots[index] = record
            try:
                record.lease = self._pool.acquire()
                record.phase = "lease"
                record.ticket = self._outbox.reserve(record.lease)
                record.phase = "outbox"
                record.seq = ledger.reserve()
                record.phase = "ready"
            except BaseException:
                self._rollback(record)
                raise
            return record
        finally:
            self._exit()

    def publish(self, record, header):
        self._enter()
        try:
            if (type(record) is not Binding or record.phase != "ready" or
                    self._slots[record.index] is not record):
                raise ValueError("Outbound binding is not ready to publish")
            try:
                prepared = self._outbox.prepare(record.ticket, header)
            except BaseException:
                self._rollback(record)
                raise
            record.ledger.publish(record.seq, header.length)
            record.published = True
            record.phase = "published"
            if not self._outbox.commit(record.ticket):
                # Commit took effect but the writer stopped. The binding and the
                # published seq are retained for epoch reclaim, never aborted as
                # an unsent reservation. Stop the outbox so no later publish
                # believes the epoch is still running.
                record.phase = "stopped"
                self._outbox.stop()
                raise RuntimeError("Body writer stopped after credit publish")
            return prepared
        finally:
            self._exit()

    def abort(self, record):
        self._enter()
        try:
            if type(record) is not Binding or self._slots[record.index] is not record:
                raise ValueError("Outbound binding is not active")
            if record.published:
                raise ValueError("Published outbound binding cannot be aborted")
            self._rollback(record)
        finally:
            self._exit()

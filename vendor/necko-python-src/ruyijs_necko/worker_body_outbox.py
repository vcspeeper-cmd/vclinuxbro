"""One bounded body outbox; dispatcher prepares before its ledger publishes."""
import struct
import threading
from .worker_body_types import BodyHeader, HandoffStopped
from .worker_body_write_types import WriteTicket, WriteCompletion, PreparedWrite, Entry
from .worker_slot_lease import SlotLease
from .worker_frames import encode_body_header
from .worker_channel_win import ticks


class BodyOutbox:
    def __init__(self, wake, stall_ms=30000):
        if type(stall_ms) is not int or not 1 <= stall_ms <= 60000:
            raise ValueError("Body writer stall must be 1..60000ms")
        self._wake, self._stall = wake, stall_ms
        self._condition = threading.Condition(threading.RLock())
        self._entries, self._ready = [None] * 64, [None] * 64
        self._head = self._tail = self._count = self._used = 0
        self._version = 0
        self._active = self._completion = self._fault = None
        self._stopped = False

    def _find(self, ticket):
        if type(ticket) is not WriteTicket:
            raise ValueError("Writer requires an actual reservation token")
        for index, entry in enumerate(self._entries):
            if entry is not None and entry.ticket is ticket:
                return index, entry
        raise ValueError("Body writer token is stale")

    def reserve(self, lease):
        if type(lease) is not SlotLease:
            raise ValueError("Writer requires an actual wire lease")
        lease.length  # Reject released leases; empty producer reservation is legal.
        ticket = WriteTicket(lease)
        candidate = Entry(ticket)
        with self._condition:
            version = self._version
            index = -1
            for position, entry in enumerate(self._entries):
                if entry is not None and entry.ticket.lease is lease:
                    raise ValueError("Wire lease already reserved by this writer")
                if entry is None:
                    index = position
            lease.length  # Allocation/GC during preparation may revoke the lease.
            # The scan/backedge may run GC. Reentry may fill a previously free
            # position; a try-admission can fail instead of displacing its owner.
            if self._stopped:
                raise HandoffStopped()
            if (version != self._version or index < 0 or
                    self._entries[index] is not None or self._used == 64):
                raise ValueError("Body writer outbox is full")
            self._entries[index] = candidate
            self._used += 1
            self._version += 1
        return ticket

    def prepare(self, ticket, header):
        if type(ticket) is not WriteTicket or type(header) is not BodyHeader:
            raise ValueError("Writer preparation requires fixed metadata")
        if ticket.lease.length != header.length:
            raise ValueError("Body header length differs from actual wire lease")
        wire = encode_body_header(header.kind, header.epoch, header.request_id,
                                  header.generation, header.hop, header.seq, header.length)
        prefix = struct.pack('<I', 48 + header.length) + wire
        prepared = PreparedWrite(header, prefix, WriteCompletion(ticket), ticks() + self._stall)
        with self._condition:
            index, entry = self._find(ticket)
            length = ticket.lease.length
            if self._stopped:
                raise HandoffStopped()
            if (self._entries[index] is not entry or entry.prepared is not None or
                    entry.published or length != header.length):
                raise ValueError("Writer token already prepared or changed")
            entry.prepared = prepared
        return prepared

    def commit(self, ticket):
        """Only after successful SenderCredit.publish; retain facts even at Stop."""
        with self._condition:
            index = 0
            while index < 64:
                entry = self._entries[index]
                if entry is not None and entry.ticket is ticket:
                    break
                index += 1
            # No tuple-return helper or object construction on this published
            # path; recheck after the scan before mutating the fixed ring.
            if (index == 64 or self._entries[index] is not entry or entry.prepared is None or
                    entry.published or self._count == 64):
                raise ValueError("Invalid body writer commit")
            tail = self._tail
            next_tail = (tail + 1) % 64
            count = self._count + 1
            entry.published = True
            self._ready[tail] = entry
            self._tail, self._count = next_tail, count
            self._condition.notify_all()
            running = not self._stopped
        self._wake.set()
        return running

    def abort(self, ticket):
        """Dispatcher may call only before publishing the corresponding seq."""
        with self._condition:
            index, entry = self._find(ticket)
            if self._entries[index] is not entry or entry.published:
                raise ValueError("Published body candidate must remain accounted")
            self._entries[index] = None
            self._used -= 1
            self._version += 1

    def facts(self, ticket):
        with self._condition:
            _, entry = self._find(ticket)
            return entry.prepared, entry.published, entry.deadline, entry.confirmed

    def snapshot(self):
        with self._condition:
            active = self._active.ticket if self._active is not None else None
            return self._used, self._count, active, self._completion, self._fault, self._stopped

    def pending_completion(self):
        with self._condition:
            return self._completion if self._active is not None and not self._active.confirmed else None

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

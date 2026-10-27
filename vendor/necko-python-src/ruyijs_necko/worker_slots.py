"""Actual fixed wire buffers, allocated before any transport grants exist."""
import threading
from .worker_slot_lease import BLOCK_SIZE, SlotLease

MAX_SLOTS = 64


class SlotLimit(ValueError):
    pass


def _allocate():
    return bytearray(BLOCK_SIZE)


def _candidate(pool):
    return SlotLease(pool)


class SlotPool:
    """One pool per epoch/direction. Live leases require explicit release."""
    def __init__(self, capacity=MAX_SLOTS):
        if type(capacity) is not int or not 1 <= capacity <= MAX_SLOTS:
            raise ValueError("Wire pool capacity must be 1..64")
        blocks = [_allocate() for _ in range(capacity)]
        leases = [None] * capacity
        self._lock = threading.RLock()
        self._capacity = capacity
        self._blocks, self._leases = blocks, leases
        self._used = self._peak = 0
        self._stopped = self._closed = False

    def acquire(self):
        # Candidate allocation may collect/reenter the pool. Read all admission
        # facts afterwards and commit to the existing fixed table only.
        lease = _candidate(self)
        with self._lock:
            if self._stopped or self._closed or self._used == self._capacity:
                raise SlotLimit("Wire pool full or stopped")
            index = 0
            while (index < self._capacity and not self._stopped and not self._closed
                   and self._leases[index] is not None):
                index += 1
            # A loop backedge may run GC finalizers. Admission is committed
            # only after rechecking their possible stop/close/acquire effects.
            if self._stopped or self._closed or self._used == self._capacity:
                raise SlotLimit("Wire pool full or stopped")
            if index == self._capacity:
                raise SlotLimit("No stable free wire slot during admission")
            used = self._used + 1
            peak = used if used > self._peak else self._peak
            lease._index = index
            self._leases[index] = lease
            self._used = used
            self._peak = peak
        return lease

    def snapshot(self):
        with self._lock:
            capacity = self._capacity
            used, peak = self._used, self._peak
            stopped, closed = self._stopped, self._closed
        return dict(capacity=capacity, live=used, peak=peak,
                    allocated_bytes=0 if closed else capacity * BLOCK_SIZE,
                    leased_bytes=used * BLOCK_SIZE, stopped=stopped, closed=closed)

    def stop(self):
        with self._lock:
            self._stopped = True

    def close(self):
        with self._lock:
            if self._used:
                raise ValueError("Wire pool close requires all leases returned")
            self._stopped = self._closed = True
            self._blocks = self._leases = None

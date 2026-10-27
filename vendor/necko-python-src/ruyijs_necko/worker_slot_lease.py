"""Access to one SlotPool-owned block; no credit, identity or protocol state."""

BLOCK_SIZE = 65536


class SlotLease:
    __slots__ = ("_pool", "_index", "_length", "_released", "_span")

    def __init__(self, pool):
        self._pool = pool
        self._index = -1
        self._length = 0
        self._released = False
        self._span = None

    def _check(self):
        pool = self._pool
        if (self._released or pool._closed or self._index < 0 or
                pool._leases[self._index] is not self):
            raise ValueError("Wire slot lease is no longer owned")

    @property
    def length(self):
        with self._pool._lock:
            self._check()
            return self._length

    def write(self, data):
        if type(data) is not bytes or not 1 <= len(data) <= BLOCK_SIZE:
            raise ValueError("Wire slot requires 1..65536 bytes")
        length = len(data)
        span = slice(0, length)
        with self._pool._lock:
            self._check()
            if self._length:
                raise ValueError("Wire slot payload is already immutable")
            self._pool._blocks[self._index][span] = data
            self._span = span
            self._length = length

    def read(self):
        with self._pool._lock:
            self._check()
            if not self._length:
                raise ValueError("Wire slot has no payload")
            # Capture the payload before releasing ownership. The detached
            # copy is bounded; bytes conversion may run GC outside the lock.
            piece = self._pool._blocks[self._index][self._span]
        return bytes(piece)

    def release(self):
        with self._pool._lock:
            if self._released:
                return False
            # No call checkpoint between the idempotence check and commit.
            pool = self._pool
            if pool._closed or self._index < 0 or pool._leases[self._index] is not self:
                raise ValueError("Wire slot lease is no longer owned")
            self._pool._leases[self._index] = None
            self._pool._used -= 1
            self._released = True
            return True

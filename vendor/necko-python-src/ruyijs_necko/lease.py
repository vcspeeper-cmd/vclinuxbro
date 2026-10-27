"""Single owner for one already-acquired Necko pool lease."""
import threading


class Lease:
    def __init__(self, pool):
        self.pool = pool
        self._lock = threading.Lock()
        self.released = False
        self.release_calls = 0

    def register(self, connection):
        self.pool.register(connection)

    def release(self, connection):
        with self._lock:
            if self.released:
                return
            self.released = True
            self.release_calls += 1
        self.pool.release(connection)

    def transfer(self, acquired_pool):
        # borrow() has already acquired the target lease.
        replacement = Lease(acquired_pool)
        self.release(None)
        return replacement


def release_failed(exchange, connection, pool, body=None):
    if body is not None:
        body.close(reuse=False)
        return
    if exchange is not None:
        exchange.close()
    if connection is not None and getattr(connection, "multiplexed", False):
        pool.release(connection)
    else:
        if connection is not None:
            connection.close()
        pool.release(None)

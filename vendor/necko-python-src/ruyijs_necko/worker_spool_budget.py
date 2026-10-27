"""Capacity charges precede real upload allocation and survive failed cleanup."""
import threading

MAX_BODY = 16 * 1024 * 1024
MAX_RETAINED = 64 * 1024 * 1024
MAX_CHUNK = 65536


class SpoolLimit(ValueError):
    pass


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid upload spool size/position")
    return value


class Accounting:
    def __init__(self, limit, max_live):
        self.limit = integer(limit, 1, MAX_RETAINED)
        self.max_live = integer(max_live, 1, 16)
        self.lock = threading.RLock()
        self.used = self.live = 0

    def reserve(self, length):
        # Construct the GC-tracked object before the critical section. Integer
        # arithmetic can allocate but cannot trigger cyclic GC; commit itself
        # replaces existing attributes only and allocates no tuple/dict/list.
        token = Charge(self, length)
        with self.lock:
            if length > self.limit - self.used or self.live >= self.max_live:
                raise SpoolLimit("Worker upload storage limit")
            used, live = self.used + length, self.live + 1
            self.used = used
            self.live = live
            token.active = True
        return token

    def release(self, token):
        with self.lock:
            if not token.active:
                return
            if self.used < token.length or self.live < 1:
                raise RuntimeError("Upload storage accounting invariant")
            used, live = self.used - token.length, self.live - 1
            self.used = used
            self.live = live
            token.active = False

    def snapshot(self):
        with self.lock:
            used, live = self.used, self.live
        return dict(bytes=used, spools=live, limit=self.limit, max_live=self.max_live)


class Charge:
    __slots__ = ("owner", "length", "active")

    def __init__(self, owner, length):
        self.owner, self.length, self.active = owner, length, False

    def release(self):
        self.owner.release(self)

    def __del__(self):
        if self.active:
            self.release()

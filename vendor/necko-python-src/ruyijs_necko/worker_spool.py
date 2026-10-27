"""Worker upload storage: real bounded buffers, sealed before replayable reads."""
import threading
from .worker_spool_budget import Accounting, SpoolLimit, integer, MAX_BODY, MAX_RETAINED, MAX_CHUNK


def _allocate(size):
    return bytearray(size)


class UploadArena:
    """One worker owner; buffers remain charged until their own close/finalizer."""
    def __init__(self, limit=MAX_RETAINED, max_live=16):
        self._accounting = Accounting(limit, max_live)

    def open(self, length):
        integer(length, 0, MAX_BODY)
        charge = self._accounting.reserve(length)
        try:
            return _UploadSpool(_allocate(length), charge)
        except BaseException:
            charge.release()
            raise

    def snapshot(self):
        return self._accounting.snapshot()


class _UploadSpool:
    """Arena factory only. No internal buffer views escape into protocol code."""
    def __init__(self, data, charge):
        self._charge = None
        self._lock = threading.RLock()
        self._data = data
        self._length = len(data)
        self._written = self._position = 0
        self._sealed = self._closed = False
        self._charge = charge

    def __len__(self):
        return self._length

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def _check(self, sealed=False):
        if self._closed or (sealed and not self._sealed):
            raise ValueError("Upload spool closed or not sealed")

    def append(self, data):
        if type(data) is not bytes or not 1 <= len(data) <= MAX_CHUNK:
            raise ValueError("Upload spool chunk must contain 1..65536 bytes")
        while True:
            with self._lock:
                self._check()
                if self._sealed or len(data) > self._length - self._written:
                    raise ValueError("Upload exceeds declared length or seal")
                written = self._written
                end = written + len(data)
            # Slice is GC-tracked. Build it outside the lock, then recheck all
            # mutable facts before touching data or committing a new cursor.
            span = slice(written, end)
            with self._lock:
                self._check()
                if self._sealed:
                    raise ValueError("Upload spool is sealed")
                if self._written != written:
                    continue
                self._data[span] = data
                self._written = end
                return

    def seal(self, total_bytes):
        integer(total_bytes, 0, MAX_BODY)
        with self._lock:
            self._check()
            if self._written != self._length or total_bytes != self._length:
                raise ValueError("Upload seal does not match declared and stored bytes")
            changed = not self._sealed
            self._sealed = True
            return changed

    def tell(self):
        with self._lock:
            self._check(sealed=True)
            return self._position

    def seek(self, offset, whence=0):
        integer(offset, -MAX_BODY, MAX_BODY)
        integer(whence, 0, 2)
        with self._lock:
            self._check(sealed=True)
            base = 0 if whence == 0 else self._position if whence == 1 else self._length
            # No callable between the current-position snapshot and commit.
            position = base + offset
            if not 0 <= position <= self._length:
                raise ValueError("Invalid upload spool position")
            self._position = position
            return position

    def read(self, size=MAX_CHUNK):
        integer(size, 0, MAX_CHUNK)
        while True:
            with self._lock:
                self._check(sealed=True)
                position = self._position
                end = min(position + size, self._length)
            span = slice(position, end)
            with self._lock:
                self._check(sealed=True)
                if self._position != position:
                    continue
                piece = self._data[span]
            # A CALL checkpoint may collect even with untracked byte objects.
            # Keep only a bounded detached copy outside the lock, then recheck
            # the cursor/lifetime before committing any read advancement.
            result = bytes(piece)
            del piece
            with self._lock:
                self._check(sealed=True)
                if self._position != position:
                    continue
                self._position = end
                return result

    def close(self):
        with self._lock:
            self._data = None
            self._closed = True
            if self._charge is not None:
                # If release fails, retain the charge and allow close to retry.
                self._charge.release()
                self._charge = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        if hasattr(self, "_lock"):
            self.close()

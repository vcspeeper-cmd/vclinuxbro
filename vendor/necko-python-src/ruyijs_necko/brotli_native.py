"""Pinned native Brotli with caller-sized output and per-instance allocator cap."""
import ctypes as ct
from pathlib import Path
import threading


class Brotli:
    def __init__(self, allocation_limit=32 * 1024 * 1024):
        if type(allocation_limit) is not int or not 1 <= allocation_limit <= 32 * 1024 * 1024:
            raise ValueError("Invalid native decoder allocation limit")
        self._lock = threading.Lock()
        path = Path(__file__).with_name("ruyijs_brotli.dll")
        self.library = ct.CDLL(str(path))
        signatures = {
            "rb_version": (ct.c_uint32, []),
            "rb_create": (ct.c_void_p, [ct.c_size_t]),
            "rb_process": (ct.c_int, [ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_size_t),
                                      ct.c_void_p, ct.POINTER(ct.c_size_t)]),
            "rb_close": (ct.c_size_t, [ct.c_void_p]),
            "rb_used": (ct.c_size_t, [ct.c_void_p]),
            "rb_peak": (ct.c_size_t, [ct.c_void_p]),
            "rb_refused": (ct.c_int, [ct.c_void_p]),
            "rb_error": (ct.c_int, [ct.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            method = getattr(self.library, name)
            method.restype, method.argtypes = result, arguments
        if self.library.rb_version() != 0x1002000:
            raise RuntimeError("Unexpected Brotli version")
        self.handle = self.library.rb_create(allocation_limit)
        self.limit, self.peak, self.remaining = allocation_limit, 0, None
        self.eof, self.needs_input = False, True
        if not self.handle:
            raise MemoryError("Brotli allocation limit or allocation failure")

    def process(self, data, maximum):
        with self._lock:
            return self._process(data, maximum)

    def _process(self, data, maximum):
        if not self.handle or self.eof:
            raise ValueError("Brotli decoder already completed")
        if not isinstance(data, bytes) or len(data) > 65536 or not 1 <= maximum <= 65536:
            raise ValueError("Brotli chunk limit exceeded")
        source = ct.create_string_buffer(data, len(data)) if data else None
        output = (ct.c_ubyte * maximum)()
        consumed, produced = ct.c_size_t(len(data)), ct.c_size_t(maximum)
        result = self.library.rb_process(self.handle, source, ct.byref(consumed), output, ct.byref(produced))
        self.peak = max(self.peak, self.library.rb_peak(self.handle))
        if result <= 0:
            if self.library.rb_refused(self.handle):
                raise MemoryError("Brotli allocator budget exceeded")
            raise ValueError(f"Invalid Brotli stream: {self.library.rb_error(self.handle)}")
        self.eof, self.needs_input = result == 1, result == 2
        assert consumed.value <= len(data) and produced.value <= maximum
        return ct.string_at(output, produced.value), data[consumed.value:]

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self.handle:
            self.peak = max(self.peak, self.library.rb_peak(self.handle))
            self.remaining = self.library.rb_close(self.handle)
            self.handle = None
            if self.remaining:
                raise RuntimeError("Brotli allocator retained live blocks")

    def __del__(self):
        if getattr(self, "handle", None):
            self.close()

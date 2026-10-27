"""Incremental compression primitives with finite output, strict EOF and windows."""
from compression import zstd
import zlib
from .brotli_native import Brotli


class Codec:
    def __init__(self, encoding, *, brotli_limit=32 * 1024 * 1024, zstd_window_log=23):
        if encoding not in ("gzip", "br", "zstd"):
            raise ValueError("Unsupported content encoding")
        if not 10 <= zstd_window_log <= 23:
            raise ValueError("Zstd window limit exceeded")
        self.encoding, self.members, self.finished = encoding, 0, False
        self._brotli_limit, self._zstd_window_log = brotli_limit, zstd_window_log
        self._pending, self._new_member, self._saw_input = b"", False, False
        self._codec = self._create()
        self.total, self.peak_native, self.closed = 0, 0, False

    def _create(self):
        if self.encoding == "gzip":
            return zlib.decompressobj(16 + zlib.MAX_WBITS)
        if self.encoding == "zstd":
            return zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: self._zstd_window_log})
        return Brotli(self._brotli_limit)

    @property
    def needs_input(self):
        if self._pending:
            return False
        if self._new_member:
            return True
        if self.encoding == "gzip":
            return not self._codec.unconsumed_tail
        return self._codec.needs_input

    def process(self, data, maximum):
        try:
            return self._process(data, maximum)
        except (zlib.error, zstd.ZstdError) as error:
            raise ValueError("Invalid " + self.encoding + " stream") from error

    def _process(self, data, maximum):
        if self.closed or self.finished:
            raise ValueError("Decoder already completed")
        if not isinstance(data, bytes) or len(data) > 65536 or not 1 <= maximum <= 65536:
            raise ValueError("Codec chunk limit exceeded")
        if data and not self.needs_input:
            raise ValueError("Decoder must drain before accepting input")
        self._saw_input |= bool(data)
        current, self._pending = self._pending or data, b""
        if self._new_member:
            if not current:
                return b""
            if self.encoding == "br":
                raise ValueError("Trailing Brotli bytes")
            self._codec = self._create()
            self._new_member = False
        if self.encoding == "br":
            output, tail = self._codec.process(current, maximum)
            self.peak_native = max(self.peak_native, self._codec.peak)
        elif self.encoding == "gzip":
            current = current or self._codec.unconsumed_tail
            output = self._codec.decompress(current, max_length=maximum)
            tail = self._codec.unused_data if self._codec.eof else b""
        else:
            output = self._codec.decompress(current, max_length=maximum)
            tail = self._codec.unused_data if self._codec.eof else b""
        if self._codec.eof:
            self.members += 1
            if self.members > 64:
                raise ValueError("Compressed member limit exceeded")
            if self.encoding == "br" and tail:
                raise ValueError("Trailing Brotli bytes")
            self._new_member = True
            self._pending = tail
        elif self.encoding == "br":
            self._pending = tail
        self.total += len(output)
        return output

    def finish(self):
        if not self._saw_input or not self._new_member or self._pending:
            raise ValueError("Truncated compressed stream")
        self.finished = True

    def close(self):
        if not self.closed:
            if self.encoding == "br":
                self._codec.close()
            self._codec = None
            self._pending = b""
            self.closed = True

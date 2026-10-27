"""One owner for a bounded encoded-read / strict decode response lifetime."""
import threading
from .decoder import Decoder, DecodeLimit


class BodyReader:
    """Consume chunks or close on the owner thread; cancellation only signals I/O."""
    def __init__(self, response, control, *, encoded_limit=Decoder.MAX_BODY,
                 decoded_limit=Decoder.MAX_BODY, intermediate_limit=Decoder.MAX_BODY,
                 chunk_size=Decoder.MAX_CHUNK):
        self._response, self.control = response, control
        self._iterator = None
        self._lock = threading.Lock()
        self.closed = False
        try:
            self.status = response.status_code
            self.version = response.raw.version
            fields, header_bytes = [], 0
            for key, value in response.raw._original_response.msg.raw_items():
                header_bytes += len(key.encode("latin1")) + len(value.encode("latin1")) + 4
                if len(fields) == 256 or header_bytes > 65536:
                    raise DecodeLimit("Response header limit exceeded after protocol parsing")
                fields.append((key, value))
            self.headers = tuple(fields)
            self.tls = getattr(response, "tls", None)
            encodings = [part.strip().lower() for value in response.raw.headers.getlist("Content-Encoding")
                         for part in value.split(",") if part.strip()]
            if response.request.method == "HEAD" or self.status in (204, 304):
                encodings = []
            self.decoder = Decoder(encodings, control, encoded_limit=encoded_limit,
                decoded_limit=decoded_limit, intermediate_limit=intermediate_limit, chunk_size=chunk_size)
            self._iterator = self.decoder.iterate(self._encoded())
        except Exception:
            response.close()
            self.closed = True
            raise

    def _encoded(self):
        while True:
            self.control.remaining("body-encoded")
            available = self.decoder.encoded_limit - self.decoder.encoded
            # At an exact boundary one scratch byte distinguishes EOF from overflow.
            size = min(self.decoder.chunk_size, available) if available else 1
            if self.decoder.decoded_limit == 0 and all(name == "identity" for name in self.decoder.encodings):
                size = 1
            data = self._response.raw.read1(size, decode_content=False)
            self.control.remaining("body-encoded")
            if not data:
                return
            if len(data) > available:
                raise DecodeLimit("Encoded body limit exceeded")
            yield data

    def read_chunk(self):
        with self._lock:
            if self.closed:
                return b""
            try:
                return next(self._iterator)
            except StopIteration:
                self._close()
                return b""
            except Exception:
                self._close()
                raise

    def _close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self._iterator is not None:
                self._iterator.close()
        finally:
            self._response.close()

    def close(self):
        with self._lock:
            self._close()

    def __iter__(self):
        return self

    def __next__(self):
        data = self.read_chunk()
        if not data:
            raise StopIteration
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_response(adapter, request, control, *, timeout=None, verify=True, cert=None,
                  proxies=None, encoded_limit=Decoder.MAX_BODY, decoded_limit=Decoder.MAX_BODY,
                  intermediate_limit=Decoder.MAX_BODY, chunk_size=Decoder.MAX_CHUNK):
    response = adapter.send(request, stream=True, control=control, timeout=timeout,
                            verify=verify, cert=cert, proxies=proxies or {})
    return BodyReader(response, control, encoded_limit=encoded_limit,
        decoded_limit=decoded_limit, intermediate_limit=intermediate_limit, chunk_size=chunk_size)

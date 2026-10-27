"""One streaming content-decoding chain, bounded at each intermediate layer."""
from .codec import Codec


class DecodeLimit(ValueError):
    pass


class Decoder:
    MAX_BODY = 16 * 1024 * 1024
    MAX_CHUNK = 65536

    def __init__(self, encodings, control, *, encoded_limit=MAX_BODY, decoded_limit=MAX_BODY,
                 intermediate_limit=MAX_BODY, chunk_size=MAX_CHUNK, brotli_limit=32 * 1024 * 1024):
        if len(encodings) > 5 or any(name not in ("gzip", "br", "zstd", "identity") for name in encodings):
            raise ValueError("Invalid content encoding chain")
        if any(type(n) is not int or not 1 <= n <= self.MAX_BODY
               for n in (encoded_limit, intermediate_limit)):
            raise ValueError("Invalid decoding byte budget")
        if type(decoded_limit) is not int or not 0 <= decoded_limit <= self.MAX_BODY:
            raise ValueError("Invalid decoding byte budget")
        if type(chunk_size) is not int or not 1 <= chunk_size <= self.MAX_CHUNK:
            raise ValueError("Invalid decoding chunk size")
        self.control, self.encodings, self.chunk_size = control, tuple(encodings), chunk_size
        self.encoded_limit, self.decoded_limit = encoded_limit, decoded_limit
        self.intermediate_limit, self.brotli_limit = intermediate_limit, brotli_limit
        self.encoded, self.decoded, self.layer_totals, self.native_peaks = 0, 0, [], []
        self._codecs, self._started = [], False

    def _encoded(self, chunks):
        for chunk in chunks:
            self.control.remaining("decode-input")
            if not isinstance(chunk, bytes) or len(chunk) > self.MAX_CHUNK:
                raise DecodeLimit("Encoded input chunk limit exceeded")
            if len(chunk) > self.encoded_limit - self.encoded:
                raise DecodeLimit("Encoded body limit exceeded")
            self.encoded += len(chunk)
            for position in range(0, len(chunk), self.chunk_size):
                yield chunk[position:position + self.chunk_size]

    def _layer(self, chunks, codec, index, limit):
        for chunk in chunks:
            data = chunk
            while True:
                self.control.remaining("decode")
                available = limit - self.layer_totals[index]
                # One byte of fixed scratch detects output beyond an exact limit.
                maximum = min(self.chunk_size, available) if available else 1
                output = codec.process(data, maximum)
                data = b""
                if len(output) > available:
                    raise DecodeLimit("Decoded/intermediate body limit exceeded")
                self.layer_totals[index] += len(output)
                if output:
                    yield output
                if codec.needs_input:
                    break
        while not codec.needs_input:
            self.control.remaining("decode-eof")
            available = limit - self.layer_totals[index]
            output = codec.process(b"", min(self.chunk_size, available) if available else 1)
            if len(output) > available:
                raise DecodeLimit("Decoded/intermediate body limit exceeded")
            self.layer_totals[index] += len(output)
            if output:
                yield output
        codec.finish()

    def iterate(self, chunks):
        if self._started:
            raise ValueError("Decoder is single-use")
        self._started = True
        stream = self._encoded(chunks)
        encodings = [e for e in reversed(self.encodings) if e != "identity"]
        try:
            for index, encoding in enumerate(encodings):
                self.control.remaining("decode-create")
                codec = Codec(encoding, brotli_limit=self.brotli_limit)
                self._codecs.append(codec)
                self.layer_totals.append(0)
                limit = self.decoded_limit if index == len(encodings) - 1 else self.intermediate_limit
                stream = self._layer(stream, codec, index, limit)
            for output in stream:
                self.control.remaining("decode-deliver")
                if len(output) > self.decoded_limit - self.decoded:
                    raise DecodeLimit("Decoded body limit exceeded")
                self.decoded += len(output)
                yield output
        finally:
            for codec in reversed(self._codecs):
                codec.close()
            self.native_peaks = [codec.peak_native for codec in self._codecs]

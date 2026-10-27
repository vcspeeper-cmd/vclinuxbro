"""Bound bytes supplied to the original exchange; no HTTP framing ownership."""
from .decoder import DecodeLimit

MAX_UPLOAD = 16 * 1024 * 1024
MAX_CHUNK = 65536


def text_bytes(text, maximum, control):
    total = 0
    if len(text) > maximum:
        raise DecodeLimit("Upload text limit exceeded")
    for index, character in enumerate(text):
        if index % 4096 == 0:
            control.remaining("upload-text")
        point = ord(character)
        if 0xD800 <= point <= 0xDFFF:
            raise UnicodeEncodeError("utf-8", text, index, index + 1, "surrogates not allowed")
        total += 1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
        if total > maximum:
            raise DecodeLimit("Upload text limit exceeded")
    return total


def headers(fields):
    from .wire_fields import WireFields, validate_framing
    if isinstance(fields, WireFields):
        validate_framing(fields)
    if len(fields) > 256 or sum(len(k) + len(v) + 4 for k, v in fields.items()) > 65536:
        raise DecodeLimit("Request header limit exceeded")


def validate(request, control):
    control.remaining("request-validate")
    from .wire_fields import WireFields, validate_framing, validate_body
    if isinstance(request.headers, WireFields):
        validate_framing(request.headers)
    headers(request.headers)
    length = request.headers.get("Content-Length")
    if length is not None and int(length) > MAX_UPLOAD:
        raise DecodeLimit("Upload body limit exceeded")
    if isinstance(request.body, (bytes, bytearray, memoryview)) and memoryview(request.body).nbytes > MAX_UPLOAD:
        raise DecodeLimit("Upload body limit exceeded")
    if isinstance(request.body, str):
        text_bytes(request.body, MAX_UPLOAD, control)
    if isinstance(request.headers, WireFields):
        validate_body(request.headers, request.body)


def parts(body, control):
    if body is None:
        return
    if isinstance(body, (str, bytes, bytearray, memoryview)):
        data = body.encode("utf-8") if isinstance(body, str) else body
        view = memoryview(data).cast("B")
        for start in range(0, len(view), MAX_CHUNK):
            yield view[start:start + MAX_CHUNK]
        return
    if hasattr(body, "read"):
        while True:
            control.remaining("upload-source")
            part = body.read(MAX_CHUNK)
            control.remaining("upload-source")
            if not part:
                return
            yield part
    else:
        iterator = iter(body)
        while True:
            control.remaining("upload-source")
            try:
                part = next(iterator)
            except StopIteration:
                return
            control.remaining("upload-source")
            yield part


def write(exchange, body, control, expected=None):
    total = 0
    for part in parts(body, control):
        control.remaining("upload")
        if isinstance(part, str):
            text_bytes(part, MAX_CHUNK, control)
            part = part.encode("utf-8")
        if not isinstance(part, (bytes, bytearray, memoryview)):
            raise TypeError("Upload source must yield byte buffers")
        part = memoryview(part).cast("B")
        if len(part) > MAX_CHUNK:
            raise DecodeLimit("Upload source chunk limit exceeded")
        if len(part) > MAX_UPLOAD - total:
            raise DecodeLimit("Upload body limit exceeded")
        if expected is not None and len(part) > expected - total:
            from requests.exceptions import InvalidHeader
            raise InvalidHeader("Upload exceeds Content-Length")
        total += len(part)
        exchange.write(part)
    control.remaining("upload-complete")
    if expected is not None and total != expected:
        from requests.exceptions import InvalidHeader
        raise InvalidHeader("Upload does not reach Content-Length")

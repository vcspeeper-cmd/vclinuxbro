"""W1 scalar constraints; no transport or browser state."""
import re
from .worker_json import U64_MAX, validate_u64

TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
REASONS = ("cancelled", "deadline", "admission", "closing", "failure")
VERSIONS = ("http/1.1", "h2", "h3")
ERROR_KINDS = ("Cancelled", "DeadlineExceeded", "DnsError", "ConnectError", "TlsError",
               "ProtocolError", "BodyLimit", "DecodeError", "WorkerExited", "AdmissionError")


def integer(value, minimum=0, maximum=4294967295):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Worker schema integer range/type")


def bounded(minimum=0, maximum=4294967295):
    return lambda value: integer(value, minimum, maximum)


def identity(value):
    validate_u64(value, minimum=1)


def watermark(value):
    if value is not None:
        validate_u64(value, maximum=U64_MAX - 1)


def boolean(value):
    if type(value) is not bool:
        raise ValueError("Worker schema boolean required")


def true(value):
    if value is not True:
        raise ValueError("Worker schema true required")


def choice(*values):
    def validate(value):
        if type(value) is not str or value not in values:
            raise ValueError("Worker schema enum invalid")
    return validate


def text(value, maximum, *, minimum=0, no_controls=False):
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise ValueError("Worker schema text size/type")
    size = 0
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF or (no_controls and (code < 32 or code == 127)):
            raise ValueError("Worker schema text character")
        size += 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
        if size > maximum:
            raise ValueError("Worker schema UTF-8 size")


def message(value):
    text(value, 512)


def url(value):
    text(value, 16384, minimum=1, no_controls=True)


def token(value, maximum):
    if type(value) is not str or not 1 <= len(value) <= maximum or not TOKEN.fullmatch(value):
        raise ValueError("Worker schema ASCII token")


def method(value):
    token(value, 32)


def phase(value):
    if type(value) is not str or not 1 <= len(value) <= 64 or not re.fullmatch(r"[a-z0-9-]+", value):
        raise ValueError("Worker schema error phase")


def hexadecimal(length, *, nonzero=False):
    def validate(value):
        if (type(value) is not str or len(value) != length or
                any(char not in "0123456789abcdef" for char in value) or
                (nonzero and not any(char != "0" for char in value))):
            raise ValueError("Worker schema hexadecimal value")
    return validate


def visible_version(value):
    if type(value) is not str or not 1 <= len(value) <= 32 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("Worker schema TLS version")


def nullable(validate):
    def optional(value):
        if value is not None:
            validate(value)
    return optional


def object_fields(value, fields):
    if type(value) is not dict or value.keys() != fields.keys():
        raise ValueError("Worker schema object fields")
    for name, validate in fields.items():
        validate(value[name])

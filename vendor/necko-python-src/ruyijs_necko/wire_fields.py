"""Bounded transient ordered fields for the existing protocol serializers."""
import re
from ipaddress import IPv6Address
from requests import PreparedRequest
from requests.exceptions import InvalidHeader
from .decoder import DecodeLimit

TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
HOSTNAME = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?\Z")


def pair(name, value):
    if isinstance(name, bytes):
        name = name.decode("ascii")
    if isinstance(value, bytes):
        value = value.decode("latin1")
    if not isinstance(name, str) or not TOKEN.fullmatch(name):
        raise InvalidHeader("Invalid wire header name")
    if not isinstance(value, str) or any(ord(c) > 255 or (ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value):
        raise InvalidHeader("Invalid wire header value")
    return name, value


class WireFields:
    """No browser state: one ordered DTO list and read/replace projections."""
    def __init__(self, fields=()):
        self._rows = []
        source = fields.items() if hasattr(fields, "items") else fields
        size = 0
        for name, value in source:
            row = pair(name, value)
            size += len(row[0]) + len(row[1]) + 4
            if len(self._rows) == 256 or size > 65536:
                raise DecodeLimit("Wire header limit exceeded")
            self._rows.append(row)

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return (name for name, _ in self._rows)

    def __contains__(self, name):
        return any(key.lower() == name.lower() for key, _ in self._rows)

    def get(self, name, default=None):
        values = self.get_all(name)
        return ", ".join(values) if values else default

    def __getitem__(self, name):
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def get_all(self, name):
        return [value for key, value in self._rows if key.lower() == name.lower()]

    def items(self):
        return iter(self._rows)

    def copy(self):
        return WireFields(self._rows)

    def __setitem__(self, name, value):
        row = pair(name, value)
        result, inserted = [], False
        for key, old in self._rows:
            if key.lower() == name.lower():
                if not inserted:
                    result.append(row)
                    inserted = True
            else:
                result.append((key, old))
        if not inserted:
            result.append(row)
        self._rows = WireFields(result)._rows

    def __delitem__(self, name):
        if name not in self:
            raise KeyError(name)
        self._rows = [(key, value) for key, value in self._rows if key.lower() != name.lower()]

    def append(self, name, value):
        self._rows = WireFields((*self._rows, pair(name, value)))._rows


def validate_host(value):
    host, port = value, None
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or not re.fullmatch(r"[0-9a-fA-F:.]+(?:%25[A-Za-z0-9_.~-]+)?", value[1:end]):
            raise InvalidHeader("Invalid IPv6 Host literal")
        try:
            IPv6Address(value[1:end])
        except ValueError as error:
            raise InvalidHeader("Invalid IPv6 Host") from error
        suffix = value[end + 1:]
        if suffix:
            if not suffix.startswith(":"):
                raise InvalidHeader("Invalid Host suffix")
            port = suffix[1:]
    else:
        if ":" in value:
            host, port = value.rsplit(":", 1)
        labels = host.removesuffix(".").split(".")
        if len(host) > 254 or any(len(label) > 63 or not HOSTNAME.fullmatch(label) for label in labels):
            raise InvalidHeader("Invalid DNS Host")
    if port is not None and (not DECIMAL.fullmatch(port) or len(port) > 5 or not 1 <= int(port) <= 65535):
        raise InvalidHeader("Invalid Host port")


def known_length(body):
    if body is None:
        return 0
    if isinstance(body, (bytes, bytearray, memoryview)):
        return memoryview(body).nbytes
    if isinstance(body, str):
        return sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in body)
    return None


def validate_framing(fields):
    for name in ("Host", "Content-Length", "Transfer-Encoding"):
        if len(fields.get_all(name)) > 1:
            raise InvalidHeader("Duplicate request framing header: " + name)
    if "Content-Length" in fields and "Transfer-Encoding" in fields:
        raise InvalidHeader("Conflicting request framing headers")
    length = fields.get("Content-Length")
    if length is not None and (len(length) > 20 or not DECIMAL.fullmatch(length) or int(length) > 2**64 - 1):
        raise InvalidHeader("Content-Length must be canonical unsigned decimal")
    encoding = fields.get("Transfer-Encoding")
    if encoding is not None and encoding.lower() != "chunked":
        raise InvalidHeader("Only chunked request Transfer-Encoding is supported")
    if "Host" in fields:
        validate_host(fields["Host"])


def validate_body(fields, body):
    length = known_length(body)
    declared = fields.get("Content-Length")
    if length is not None and declared is not None and int(declared) != length:
        raise InvalidHeader("Content-Length does not match known body bytes")


def prepare_request(method, url, fields=(), body=None):
    explicit = WireFields(fields)
    validate_framing(explicit)
    request = PreparedRequest()
    request.prepare(method=method, url=url, headers={}, data=body)
    generated = WireFields(request.headers)
    size = known_length(request.body)
    if size is not None and "Content-Length" in generated:
        generated["Content-Length"] = str(size)
    for name, value in generated.items():
        if name not in explicit:
            explicit.append(name, value)
    validate_framing(explicit)
    validate_body(explicit, request.body)
    request.headers = explicit
    return request

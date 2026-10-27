"""W1 compound DTO validation; preserves every input field and order."""
import base64
import binascii
from .worker_schema_types import (ERROR_KINDS, VERSIONS, bounded, choice, identity,
    integer, message, nullable, object_fields, phase, token, visible_version)


def key(value):
    object_fields(value, dict(requestId=identity, generation=bounded(1), hop=bounded(0, 19)))


def error(value):
    object_fields(value, dict(kind=choice(*ERROR_KINDS), phase=phase, code=integer,
                             message=message, attempt=bounded(0, 3)))


def tls(value):
    object_fields(value, dict(backend=choice("nss"), alpn=choice(*VERSIONS), version=visible_version))


def options(value):
    object_fields(value, dict(trustProfile=integer, proxyProfile=nullable(integer),
                             connectMs=bounded(1, 60000), readMs=bounded(1, 60000)))


def headers(value):
    if type(value) is not list or len(value) > 256:
        raise ValueError("Worker schema header count/type")
    size = 0
    for row in value:
        if type(row) is not dict or row.keys() != {"name", "valueBase64"}:
            raise ValueError("Worker schema header fields")
        name, encoded = row["name"], row["valueBase64"]
        token(name, 65532)
        if type(encoded) is not str or len(encoded) % 4 or len(encoded) > 87384:
            raise ValueError("Worker schema base64 size/type")
        padding = 2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0
        decoded_size = len(encoded) // 4 * 3 - padding
        if size + len(name) + decoded_size + 4 > 65536:
            raise ValueError("Worker schema header bytes")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Worker schema base64 alphabet/padding") from None
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise ValueError("Worker schema noncanonical base64")
        if any((byte < 32 and byte != 9) or byte == 127 for byte in raw):
            raise ValueError("Worker schema header value character")
        size += len(name) + len(raw) + 4

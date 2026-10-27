"""W0 strict JSON/envelope codec; payload schemas and channel state are W1."""
import json
import struct
from .worker_json_scan import MAX_CONTROL, MAX_DEPTH, MAX_NODES, preflight

U64_MAX = (1 << 64) - 1
TYPES = frozenset(("hello", "ready", "reserve", "accepted", "activate", "activated",
    "release-reservation", "reservationReleased", "ack-reservation", "upload-credit",
    "upload-end", "headers", "download-credit", "body-end", "cancel", "terminal",
    "released", "ack-terminal", "close", "closed", "fatal"))
FIELDS = frozenset(("version", "type", "epoch", "channelSeq", "payload"))


def validate_u64(value, *, minimum=0, maximum=U64_MAX):
    if (type(value) is not str or not 1 <= len(value) <= 20 or
            any(c < "0" or c > "9" for c in value) or
            (len(value) > 1 and value[0] == "0")):
        raise ValueError("Noncanonical worker unsigned64")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError("Worker unsigned64 out of range")
    return number


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate worker JSON key")
        result[key] = value
    return result


def _envelope(value):
    if type(value) is not dict or value.keys() != FIELDS:
        raise ValueError("Invalid worker envelope fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Invalid worker protocol version")
    if type(value["type"]) is not str or value["type"] not in TYPES:
        raise ValueError("Invalid worker message type")
    validate_u64(value["epoch"], minimum=1)
    validate_u64(value["channelSeq"], maximum=U64_MAX - 1)
    if type(value["payload"]) is not dict:
        raise ValueError("Worker payload must be an object")
    return value


def _tree(value):
    nodes = wire_bytes = 0

    def add(size):
        nonlocal wire_bytes
        wire_bytes += size
        if wire_bytes > MAX_CONTROL:
            raise ValueError("Worker JSON size limit")

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES:
            raise ValueError("Worker JSON node limit")
        kind = type(item)
        if kind in (dict, list):
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("Worker JSON depth limit")
            add(2 + max(0, len(item) - 1) + (len(item) if kind is dict else 0))
            for key in item:
                if kind is dict:
                    if type(key) is not str:
                        raise ValueError("Worker JSON key must be a string")
                    visit(key, depth)
                    visit(item[key], depth)
                else:
                    visit(key, depth)
        elif kind is str:
            if len(item) > MAX_CONTROL:
                raise ValueError("Worker JSON text limit")
            add(2)
            for char in item:
                code = ord(char)
                if 0xD800 <= code <= 0xDFFF:
                    raise ValueError("Invalid worker Unicode scalar")
                if code in (34, 92, 8, 12, 10, 13, 9):
                    add(2)
                elif code < 32:
                    add(6)
                else:
                    add(1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4)
        elif kind is int:
            if not 0 <= item <= 4294967295:
                raise ValueError("Worker JSON unsigned32 out of range")
            add(len(str(item)))
        elif kind in (bool, type(None)):
            add(5 if item is False else 4)
        else:
            raise ValueError("Invalid worker JSON value type")
    visit(value, 0)


def decode_json(data):
    preflight(data)
    try:
        text = data.decode("utf-8", "strict")
        value = json.loads(text, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Invalid worker UTF-8 JSON") from None
    return _envelope(value)


def encode_json(value):
    _envelope(value)
    _tree(value)
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    output = bytearray()
    for piece in encoder.iterencode(value):
        encoded = piece.encode("utf-8", "strict")
        if len(output) + len(encoded) > MAX_CONTROL:
            raise ValueError("Worker JSON size limit")
        output.extend(encoded)
    return bytes(output)


def decode_control_frame(frame):
    if type(frame) is not bytes or not 5 <= len(frame) <= MAX_CONTROL + 4:
        raise ValueError("Worker control frame size/type invalid")
    size = struct.unpack_from("<I", frame)[0]
    if size != len(frame) - 4:
        raise ValueError("Worker control frame length mismatch")
    return decode_json(frame[4:])


def encode_control_frame(value):
    data = encode_json(value)
    return struct.pack("<I", len(data)) + data

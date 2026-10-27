"""Stateless W0 little-endian framing; no pipe, peer authentication or credit."""
import struct

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1
PREFACE_MAGIC = 0x48575952
BODY_MAGIC = 0x42575952
PREFACE = struct.Struct("<IHHQII16s")
BODY = struct.Struct("<IHHQQIIQII")


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid worker integer")
    return value


def encode_preface(role, epoch, pid, nonce):
    _integer(role, 1, 2)
    _integer(epoch, 1, U64_MAX)
    _integer(pid, 1, U32_MAX)
    if type(nonce) is not bytes or len(nonce) != 16 or not any(nonce):
        raise ValueError("Invalid worker nonce")
    return PREFACE.pack(PREFACE_MAGIC, 1, role, epoch, pid, 0, nonce)


def decode_preface(data):
    if type(data) is not bytes or len(data) != PREFACE.size:
        raise ValueError("Invalid worker preface size/type")
    magic, version, role, epoch, pid, reserved, nonce = PREFACE.unpack(data)
    if magic != PREFACE_MAGIC or version != 1 or reserved:
        raise ValueError("Invalid worker preface constants")
    encode_preface(role, epoch, pid, nonce)
    return dict(role=role, epoch=epoch, pid=pid, nonce=nonce)


def encode_body_header(body_type, epoch, request_id, generation, hop, seq, payload_length):
    _integer(body_type, 1, 2)
    _integer(epoch, 1, U64_MAX)
    _integer(request_id, 1, U64_MAX)
    _integer(generation, 1, U32_MAX)
    _integer(hop, 0, 19)
    _integer(seq, 0, U64_MAX - 1)
    _integer(payload_length, 1, 65536)
    return BODY.pack(BODY_MAGIC, 1, body_type, epoch, request_id, generation,
                     hop, seq, payload_length, 0)


def decode_body_header(total_length, data):
    _integer(total_length, 49, 65584)
    if type(data) is not bytes or len(data) != BODY.size:
        raise ValueError("Invalid worker body header size/type")
    fields = BODY.unpack(data)
    magic, version, body_type, epoch, request_id, generation, hop, seq, length, flags = fields
    if magic != BODY_MAGIC or version != 1 or flags or total_length != BODY.size + length:
        raise ValueError("Invalid worker body header constants/length")
    encode_body_header(body_type, epoch, request_id, generation, hop, seq, length)
    return dict(type=body_type, epoch=epoch, requestId=request_id, generation=generation,
                hop=hop, seq=seq, payloadLength=length, flags=flags)


def encode_body_frame(body_type, epoch, request_id, generation, hop, seq, payload):
    if type(payload) is not bytes:
        raise ValueError("Worker payload must be bytes")
    header = encode_body_header(body_type, epoch, request_id, generation, hop, seq, len(payload))
    return struct.pack("<I", len(header) + len(payload)) + header + payload


def decode_body_frame(frame):
    if type(frame) is not bytes or not 53 <= len(frame) <= 65588:
        raise ValueError("Invalid worker body frame size/type")
    total = struct.unpack_from("<I", frame)[0]
    if total != len(frame) - 4:
        raise ValueError("Worker body frame length mismatch")
    value = decode_body_header(total, frame[4:52])
    value["payload"] = frame[52:]
    return value

"""Immutable current-operation facts, without domain identity or credit state."""
from dataclasses import dataclass
import struct
from .worker_frames import decode_body_header


@dataclass(frozen=True, slots=True)
class BodyHeader:
    kind: int
    epoch: int
    request_id: int
    generation: int
    hop: int
    seq: int
    length: int

    @classmethod
    def decode(cls, prefix_and_header):
        if type(prefix_and_header) is not bytes or len(prefix_and_header) != 52:
            raise ValueError("Body prefix/header must contain exactly 52 bytes")
        fields = decode_body_header(struct.unpack_from('<I', prefix_and_header)[0], prefix_and_header[4:])
        return cls(fields['type'], fields['epoch'], fields['requestId'], fields['generation'],
                   fields['hop'], fields['seq'], fields['payloadLength'])


@dataclass(frozen=True, slots=True, eq=False)
class HeaderRequest:
    header: BodyHeader
    deadline: int


@dataclass(frozen=True, slots=True, eq=False)
class BodyCompletion:
    request: HeaderRequest
    lease: object


@dataclass(frozen=True, slots=True)
class BodyFault:
    phase: str
    kind: str
    code: int


class HandoffStopped(Exception):
    pass

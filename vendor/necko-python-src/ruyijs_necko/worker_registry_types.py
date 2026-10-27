"""Finite immutable transport admission values, independent of message schemas."""
from dataclasses import asdict, dataclass

U64_MAX = (1 << 64) - 1
REASONS = frozenset(("cancelled", "deadline", "admission", "closing", "failure"))


def integer(value, low=0, high=U64_MAX):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid registry integer")
    return value


def key_value(key):
    if type(key) is not tuple or len(key) != 3:
        raise ValueError("Registry key must be an immutable triple")
    integer(key[0], 1)
    integer(key[1], 1, (1 << 32) - 1)
    integer(key[2], 0, 19)
    return key


def reason_value(reason):
    if type(reason) is not str or reason not in REASONS:
        raise ValueError("Invalid registry reason")
    return reason


@dataclass(frozen=True)
class Ticket:
    reservation_id: int
    partition: int
    deadline: int
    phase: str = "reserved"
    key: tuple | None = None
    cancel_reason: str | None = None
    terminal: str | None = None


def event(name, ticket=None, *, changed=True, **extra):
    result = dict(event=name, changed=changed, **extra)
    if ticket is not None:
        result["ticket"] = asdict(ticket)
    return result

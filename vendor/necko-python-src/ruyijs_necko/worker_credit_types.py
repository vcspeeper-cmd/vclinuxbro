"""Immutable ledger values and shared range checks, with no payload allocation."""
from dataclasses import dataclass
from threading import get_ident

MAX_SEQ = (1 << 64) - 2


def number(value, low=0, high=MAX_SEQ):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid credit integer")
    return value


def mark(value):
    return -1 if value is None else number(value)


def external(value):
    return None if value == -1 else value


def boolean(value):
    if type(value) is not bool:
        raise ValueError("Invalid credit boolean")
    return value


@dataclass(frozen=True)
class State:
    grant: int = -1
    ack: int = -1
    done: int = -1
    pending: int | None = None
    stopped: bool = False
    sealed: bool = False
    last: int = -1


class Ledger:
    def __init__(self):
        self._thread = get_ident()
        self._state = State()

    def _check(self):
        if get_ident() != self._thread:
            raise ValueError("Credit ledger requires its dispatcher")

    def _pending(self, seq):
        self._check()
        number(seq)
        if self._state.pending != seq:
            raise ValueError("Unknown credit reservation")

    def snapshot(self):
        self._check()
        state = self._state
        return dict(grantThrough=external(state.grant), ackedThrough=external(state.ack),
                    completedThrough=external(state.done), pending=state.pending, stop=state.stopped,
                    sealed=state.sealed, lastSeq=external(state.last), drained=self.drained,
                    occupied=self.occupied, window=self.window)

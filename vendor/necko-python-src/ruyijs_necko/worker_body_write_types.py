"""Fixed-size writer identities and prepared facts; no payload or credit owner."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, eq=False)
class WriteTicket:
    lease: object


@dataclass(frozen=True, slots=True, eq=False)
class WriteCompletion:
    ticket: WriteTicket


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    header: object
    prefix: bytes
    completion: WriteCompletion
    queued_deadline: int


class Entry:
    __slots__ = ('ticket', 'prepared', 'published', 'deadline', 'confirmed')

    def __init__(self, ticket):
        self.ticket = ticket
        self.prepared = None
        self.published = self.confirmed = False
        self.deadline = 0

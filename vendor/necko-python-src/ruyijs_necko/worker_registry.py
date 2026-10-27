"""Single-dispatcher finite admission; no metadata, network or domain request IDs."""
from dataclasses import asdict, replace
from threading import get_ident
from .worker_registry_lifecycle import Lifecycle
from .worker_registry_types import Ticket, event, integer, key_value


class Registry(Lifecycle):
    def __init__(self, epoch):
        self.epoch = integer(epoch, 1)
        self._thread = get_ident()
        self._state = ({}, 0, False, False)

    def _check(self):
        if get_ident() != self._thread or self._state[3]:
            raise ValueError("Registry dispatcher or epoch is invalid")

    def _by_id(self, reservation_id):
        integer(reservation_id, 1)
        try:
            return self._state[0][reservation_id]
        except KeyError as error:
            raise ValueError("Unknown reservation") from error

    def _by_key(self, key):
        key_value(key)
        for ticket in self._state[0].values():
            if ticket.key == key:
                return ticket
        raise ValueError("Unknown request key")

    def _replace(self, ticket, name, *, move=False):
        rows, maximum, closing, dead = self._state
        updated = dict(rows)
        if move:
            del updated[ticket.reservation_id]
        updated[ticket.reservation_id] = ticket
        result = event(name, ticket)
        self._state = (updated, maximum, closing, dead)
        return result

    def _delete(self, ticket, name):
        rows, maximum, closing, dead = self._state
        updated = dict(rows)
        del updated[ticket.reservation_id]
        result = event(name, ticket)
        self._state = (updated, maximum, closing, dead)
        return result

    def counts(self):
        rows = self._state[0].values()
        return dict(tickets=len(self._state[0]), active=sum(t.phase == "active" for t in rows),
                    waiting=sum(t.phase in ("reserved", "queued") for t in rows))

    def snapshot(self):
        if get_ident() != self._thread:
            raise ValueError("Registry snapshot requires dispatcher")
        rows, maximum, closing, dead = self._state
        return dict(epoch=self.epoch, max_seen=maximum, closing=closing, dead=dead,
                    counts=self.counts(), tickets=[asdict(t) for t in rows.values()])

    def reserve(self, reservation_id, partition, deadline_qpc, now_qpc):
        self._check()
        integer(reservation_id, 1)
        integer(partition, 0, 3)
        integer(deadline_qpc, 1)
        integer(now_qpc)
        rows, maximum, closing, dead = self._state
        if reservation_id <= maximum:
            raise ValueError("Reservation identity was already consumed")
        counts = self.counts()
        reason = "closing" if closing else "deadline" if deadline_qpc <= now_qpc else None
        if reason is None and (counts["tickets"] >= 80 or counts["waiting"] >= 64):
            reason = "admission"
        if reason is not None:
            result = event("rejected", reservation_id=reservation_id, reason=reason)
            self._state = (rows, reservation_id, closing, dead)
            return result
        ticket = Ticket(reservation_id, partition, deadline_qpc)
        updated = dict(rows)
        updated[reservation_id] = ticket
        result = event("accepted", ticket)
        self._state = (updated, reservation_id, closing, dead)
        return result

    def activate(self, reservation_id, key, now_qpc):
        self._check()
        key_value(key)
        integer(now_qpc)
        ticket = self._by_id(reservation_id)
        if ticket.key is not None:
            if ticket.key != key:
                raise ValueError("Reservation already has a different key")
            name = "released" if ticket.phase == "released" else "terminal" if ticket.terminal else "activated"
            return event(name, ticket, changed=False)
        if ticket.phase == "reservationReleased":
            return event("reservationReleased", ticket, changed=False)
        if ticket.deadline <= now_qpc:
            return self._replace(replace(ticket, phase="reservationReleased", cancel_reason="deadline"),
                                 "reservationReleased")
        if any(item.key == key for item in self._state[0].values()):
            raise ValueError("Request key is already registered")
        return self._replace(replace(ticket, key=key, phase="queued"), "activated", move=True)

    def claim(self, now_qpc):
        self._check()
        integer(now_qpc)
        if self._state[2] or self.counts()["active"] >= 16:
            return None
        for ticket in self._state[0].values():
            if ticket.phase == "queued" and ticket.deadline > now_qpc:
                return self._replace(replace(ticket, phase="active"), "claimed")
        return None

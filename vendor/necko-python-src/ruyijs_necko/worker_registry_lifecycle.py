"""Terminal, verified cleanup and acknowledgement transitions for one registry."""
from dataclasses import replace
from .worker_registry_types import event, integer, reason_value


class Lifecycle:
    def release_reservation(self, reservation_id, reason):
        self._check()
        reason_value(reason)
        ticket = self._by_id(reservation_id)
        if ticket.key is not None:
            raise ValueError("Activated request requires cancel")
        if ticket.phase == "reservationReleased":
            return event("reservationReleased", ticket, changed=False)
        updated = replace(ticket, phase="reservationReleased", cancel_reason=reason)
        return self._replace(updated, "reservationReleased")

    def ack_reservation(self, reservation_id):
        self._check()
        ticket = self._by_id(reservation_id)
        if ticket.phase != "reservationReleased" or ticket.key is not None:
            raise ValueError("Reservation release has not completed")
        return self._delete(ticket, "reservationAcked")

    def cancel(self, key, reason):
        self._check()
        reason_value(reason)
        ticket = self._by_key(key)
        if ticket.cancel_reason is not None or ticket.terminal is not None:
            return event("cancelRequested", ticket, changed=False)
        updated = replace(ticket, cancel_reason=reason)
        if ticket.phase == "queued":
            updated = replace(updated, phase="terminal", terminal="cancelled")
        return self._replace(updated, "terminal" if updated.terminal else "cancelRequested")

    def terminal(self, key, outcome):
        self._check()
        if type(outcome) is not str or outcome not in ("complete", "error", "cancelled"):
            raise ValueError("Invalid terminal outcome")
        ticket = self._by_key(key)
        if ticket.terminal is not None:
            if ticket.terminal != outcome:
                raise ValueError("Conflicting terminal outcome")
            return event("terminal", ticket, changed=False)
        if ticket.phase != "active":
            raise ValueError("Only active execution may produce terminal")
        return self._replace(replace(ticket, terminal=outcome), "terminal")

    def released(self, key, *, lease_closed, interrupts, local_resources_released):
        self._check()
        if lease_closed is not True or local_resources_released is not True or type(interrupts) is not int or interrupts != 0:
            raise ValueError("Explicit successful local cleanup is required")
        ticket = self._by_key(key)
        if ticket.terminal is None:
            raise ValueError("Terminal must precede resource release")
        if ticket.phase == "released":
            return event("released", ticket, changed=False)
        return self._replace(replace(ticket, phase="released"), "released")

    def ack_terminal(self, key, *, upload_drained, download_drained, disposition):
        self._check()
        if upload_drained is not True or download_drained is not True:
            raise ValueError("Both wire ledgers must be drained")
        if type(disposition) is not str or disposition not in ("accepted", "discarded"):
            raise ValueError("Invalid terminal disposition")
        ticket = self._by_key(key)
        if ticket.phase != "released":
            raise ValueError("Resources remain registered")
        return self._delete(ticket, "terminalAcked")

    def _stop_ticket(self, ticket, reason):
        if ticket.phase == "reserved":
            return replace(ticket, phase="reservationReleased", cancel_reason=reason)
        if ticket.phase == "queued":
            return replace(ticket, phase="terminal", terminal="cancelled", cancel_reason=reason)
        if ticket.phase == "active" and ticket.cancel_reason is None and ticket.terminal is None:
            return replace(ticket, cancel_reason=reason)
        return ticket

    def expire(self, now_qpc):
        self._check()
        integer(now_qpc)
        rows, maximum, closing, dead = self._state
        updated, result = dict(rows), []
        for reservation_id, ticket in rows.items():
            if ticket.deadline <= now_qpc:
                item = self._stop_ticket(ticket, "deadline")
                if item != ticket:
                    updated[reservation_id] = item
                    result.append(event("expired", item))
        self._state = (updated, maximum, closing, dead)
        return result

    def close(self, reason="closing"):
        self._check()
        reason_value(reason)
        rows, maximum, _, dead = self._state
        updated, result = {}, []
        for reservation_id, ticket in rows.items():
            item = self._stop_ticket(ticket, reason)
            updated[reservation_id] = item
            if item != ticket:
                result.append(event("closing", item))
        self._state = (updated, maximum, True, dead)
        return result

    def teardown(self, *, process_exit_confirmed):
        self._check()
        if process_exit_confirmed is not True:
            raise ValueError("Epoch cleanup requires confirmed process exit")
        result = event("epochClosed", tickets=len(self._state[0]))
        self._state = ({}, self._state[1], True, True)
        return result

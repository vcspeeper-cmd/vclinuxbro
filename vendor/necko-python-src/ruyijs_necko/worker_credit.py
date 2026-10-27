"""One-direction sender credit: reserve before producing, publish before ack."""
from dataclasses import replace
from .worker_credit_types import Ledger, boolean, external, mark, number, MAX_SEQ


class SenderCredit(Ledger):
    def update(self, grant_through, acked_through, stop=False):
        self._check()
        grant, ack = mark(grant_through), mark(acked_through)
        boolean(stop)
        state = self._state
        if grant < state.grant or ack < state.ack or ack > state.done:
            raise ValueError("Credit grant/ack regressed or acknowledges unpublished data")
        if not 0 <= grant - ack <= 4 or (state.stopped and not stop):
            raise ValueError("Invalid credit window or stop reversal")
        # Credit issued before peer observed seal may arrive late; it grants no new production.
        self._state = replace(state, grant=grant, ack=ack, stopped=stop)

    def reserve(self):
        self._check()
        state = self._state
        seq = state.done + 1
        if state.stopped or state.sealed or state.pending is not None or seq > state.grant or seq > MAX_SEQ:
            raise ValueError("No producer credit reservation available")
        self._state = replace(state, pending=seq)
        return seq

    def publish(self, seq, payload_length):
        self._pending(seq)
        number(payload_length, 1, 65536)
        state = self._state
        if state.stopped or state.sealed:
            raise ValueError("Stopped producer must abort its unpublished reservation")
        self._state = replace(state, pending=None, done=seq)

    def abort(self, seq):
        self._pending(seq)
        self._state = replace(self._state, pending=None)

    def seal(self, last_seq):
        self._check()
        last = mark(last_seq)
        state = self._state
        if state.pending is not None or last != state.done:
            raise ValueError("Seal requires exactly all published data and no reservation")
        if state.sealed:
            if last != state.last:
                raise ValueError("Conflicting producer seal")
            return False
        self._state = replace(state, sealed=True, last=last)
        return True

    @property
    def drained(self):
        state = self._state
        return state.sealed and state.pending is None and state.ack == state.done

    @property
    def occupied(self):
        state = self._state
        return state.done - state.ack + (state.pending is not None)

    @property
    def window(self):
        state = self._state
        return (state.done if state.sealed else state.grant) - state.ack

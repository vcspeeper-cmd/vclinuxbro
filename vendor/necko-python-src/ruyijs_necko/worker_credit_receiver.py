"""One-direction receiver credit: authorized header, payload receipt, consumption."""
from dataclasses import replace
from .worker_credit_types import Ledger, external, mark, number


class ReceiverCredit(Ledger):
    def grant(self, grant_through):
        """Caller must reserve real buffer/store before committing this pure counter."""
        self._check()
        grant = mark(grant_through)
        state = self._state
        if grant < state.grant or not 0 <= grant - state.ack <= 4:
            raise ValueError("Invalid cumulative receiver credit")
        if grant != state.grant and (state.stopped or state.sealed):
            raise ValueError("Stopped receiver cannot extend credit")
        self._state = replace(state, grant=grant)

    def receive(self, seq, payload_length):
        self._check()
        number(seq)
        number(payload_length, 1, 65536)
        state = self._state
        if state.pending is not None or seq != state.done + 1 or seq > state.grant:
            raise ValueError("Frame is unordered or has no credit")
        if state.sealed and seq > state.last:
            raise ValueError("Frame exceeds stream seal")
        self._state = replace(state, pending=seq)

    def received(self, seq):
        """Called after the complete reserved payload has been read or discarded."""
        self._pending(seq)
        self._state = replace(self._state, pending=None, done=seq)

    def abort(self, seq):
        """Only before payload I/O; a partial read instead fails the channel epoch."""
        self._pending(seq)
        self._state = replace(self._state, pending=None)

    def consume(self, through):
        self._check()
        ack = mark(through)
        state = self._state
        if ack < state.ack or ack > state.done or ack > state.ack + 1:
            raise ValueError("Consume the next received block without skipping")
        self._state = replace(state, ack=ack)

    def stop(self):
        self._check()
        self._state = replace(self._state, stopped=True)

    def seal(self, last_seq):
        self._check()
        last = mark(last_seq)
        state = self._state
        high = state.done if state.pending is None else state.pending
        if last < high or last > state.grant:
            raise ValueError("Seal conflicts with received/reserved data or credit")
        if state.sealed:
            if last != state.last:
                raise ValueError("Conflicting receiver seal")
            return False
        self._state = replace(state, sealed=True, last=last)
        return True

    def credit(self):
        self._check()
        state = self._state
        return dict(grantThrough=external(state.grant), ackedThrough=external(state.ack), stop=state.stopped)

    @property
    def drained(self):
        state = self._state
        return state.sealed and state.pending is None and state.ack == state.last

    @property
    def occupied(self):
        state = self._state
        return state.done - state.ack + (state.pending is not None)

    @property
    def window(self):
        state = self._state
        return 0 if self.drained else state.grant - state.ack

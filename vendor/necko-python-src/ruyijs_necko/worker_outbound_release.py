"""Completion and credit reclaim for dispatcher-owned outbound bindings."""
from .worker_outbound import OutboundBindings
from .worker_body_write_handoff import confirm
from .worker_body_write_types import WriteCompletion
from .worker_credit import SenderCredit


def _enter(bindings):
    if type(bindings) is not OutboundBindings:
        raise ValueError("Outbound release requires the binding owner")
    bindings._enter()


def finish(bindings, completion):
    _enter(bindings)
    try:
        current = bindings._outbox.pending_completion()
        if type(completion) is not WriteCompletion or current is not completion:
            raise ValueError("Outbound completion is stale")
        record = _match(bindings, completion.ticket)
        if not record.published or record.local_done:
            raise ValueError("Outbound completion has no published binding")
        record.local_done = True
        confirm(bindings._outbox, completion)
        record.confirmed = True
        _release_if_acked(bindings, record)
    finally:
        bindings._exit()


def apply_credit(bindings, ledger, grant, ack, stop=False):
    _enter(bindings)
    try:
        if type(ledger) is not SenderCredit:
            raise ValueError("Outbound credit requires the sender ledger")
        ledger.update(grant, ack, stop)
        for index, record in enumerate(bindings._slots):
            if record is not None and record.ledger is ledger:
                _release_if_acked(bindings, record, index)
    finally:
        bindings._exit()


def reclaim(bindings, joined):
    """Epoch failure recovery: return leases the stopped writer no longer reads.

    Only legal once the local writer has really joined, so no block is still
    being written. Published-but-unacked bindings are reclaimed here because no
    remote ack will arrive for a failed epoch; this is local memory recovery and
    does not assert that the remote Job exited.
    """
    _enter(bindings)
    try:
        if joined is not True:
            raise ValueError("Outbound reclaim requires a joined writer")
        released = 0
        for index, record in enumerate(bindings._slots):
            if record is None:
                continue
            if record.lease is not None and record.lease.release():
                released += 1
            record.lease = None
            bindings._slots[index] = None
        return released
    finally:
        bindings._exit()


def _match(bindings, ticket):
    for record in bindings._slots:
        if record is not None and record.ticket is ticket:
            return record
    raise ValueError("Outbound completion token is stale")


def _release_if_acked(bindings, record, index=None):
    if not (record.published and record.local_done and record.confirmed):
        return
    acked = record.ledger.snapshot()["ackedThrough"]
    if acked is None or record.seq > acked:
        return
    if index is None:
        index = record.index
    if bindings._slots[index] is not record:
        return
    if record.lease.release():
        bindings._slots[index] = None

"""Single consumer transitions on BodyOutbox, with no separate state owner."""
from .worker_body_types import HandoffStopped
from .worker_body_write_types import WriteCompletion
from .worker_channel_win import ticks


def take(outbox):
    with outbox._condition:
        while True:
            now = ticks()
            deadline = now + outbox._stall
            if outbox._stopped:
                raise HandoffStopped()
            if outbox._active is not None:
                raise ValueError("Writer already holds an active candidate")
            if outbox._count:
                entry = outbox._ready[outbox._head]
                if now >= entry.prepared.queued_deadline:
                    raise TimeoutError("Body candidate queue deadline elapsed")
                head = outbox._head
                outbox._ready[head] = None
                outbox._head = (head + 1) % 64
                outbox._count -= 1
                entry.deadline = deadline
                outbox._active = entry
                outbox._completion = None
                return entry
            # Condition's own waiter allocation can run GC before registration.
            outbox._condition.wait(.05)


def complete(outbox, entry):
    with outbox._condition:
        # Successful writes remain facts when Stop races after kernel completion.
        if outbox._active is not entry or outbox._completion is not None:
            raise ValueError("Body writer completion has no matching active operation")
        outbox._completion = entry.prepared.completion
    outbox._wake.set()


def confirm(outbox, completion):
    if type(completion) is not WriteCompletion:
        raise ValueError("Writer confirmation requires its actual completion")
    with outbox._condition:
        entry = outbox._active
        if (outbox._stopped or entry is None or completion is not outbox._completion or
                entry.confirmed):
            raise ValueError("Body writer confirmation is stale, stopped or expired")
        index, found = outbox._find(completion.ticket)
        now = ticks()
        if (outbox._stopped or outbox._active is not entry or found is not entry or
                outbox._entries[index] is not entry or entry.confirmed or now >= entry.deadline):
            raise ValueError("Body writer operation changed during confirmation")
        entry.confirmed = True
        outbox._entries[index] = None
        outbox._used -= 1
        outbox._version += 1
        outbox._condition.notify_all()


def wait_confirmation(outbox, entry):
    with outbox._condition:
        while True:
            if outbox._active is not entry:
                raise ValueError("Body writer active candidate changed")
            if entry.confirmed:
                outbox._active = outbox._completion = None
                return
            if outbox._stopped:
                raise HandoffStopped()
            remaining = entry.deadline - ticks()
            # A GC finalizer may Stop/confirm at this call checkpoint. Its
            # notify precedes wait, so repeat the predicate to avoid lost wake.
            if outbox._active is not entry:
                raise ValueError("Body writer active candidate changed")
            if entry.confirmed:
                continue
            if outbox._stopped:
                raise HandoffStopped()
            if remaining <= 0:
                raise TimeoutError("Body writer completion deadline elapsed")
            outbox._condition.wait(min(remaining / 1000, .05))

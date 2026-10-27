"""One fixed outbound binding; phases are cleanup facts, not a second credit ledger."""


class Binding:
    __slots__ = ("owner", "ledger", "lease", "ticket", "seq", "published",
                 "local_done", "confirmed", "phase", "index")

    def __init__(self, owner, ledger):
        self.owner = owner
        self.ledger = ledger
        self.lease = self.ticket = self.seq = None
        self.published = self.local_done = self.confirmed = False
        self.phase = "placeholder"
        self.index = -1

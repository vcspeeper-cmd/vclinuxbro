"""Epoch-scoped partition composition, shared Resolver and physical budget."""
import threading
import time
from .connection_budget import AdmissionError, ConnectionBudget
from .resolver import Resolver


def _profile(value):
    if type(value) is not tuple or len(value) != 2:
        raise ValueError("Partition profile pair required")
    trust, proxy = value
    if type(trust) is not int or not 0 <= trust <= 0xffffffff:
        raise ValueError("Trust profile identity")
    if proxy is not None and (type(proxy) is not int or not 0 <= proxy <= 0xffffffff):
        raise ValueError("Proxy profile identity")
    return value


class TransportResources:
    def __init__(self, partitions):
        if type(partitions) is not dict or not 1 <= len(partitions) <= 4:
            raise ValueError("One to four explicit partitions required")
        configs = {}
        for partition, profile in partitions.items():
            if type(partition) is not int or not 0 <= partition <= 3:
                raise ValueError("Partition identity")
            configs[partition] = _profile(profile)
        self._profiles, self._adapters = configs, [None] * 4
        self._creating = [False] * 4
        self._lock, self._closing = threading.Condition(), False
        self.budget, self.resolver = ConnectionBudget(), Resolver()

    def adapter(self, partition, profile):
        profile = _profile(profile)
        if type(partition) is not int or self._profiles.get(partition) != profile:
            raise ValueError("Unregistered partition/profile")
        with self._lock:
            rejected = self._closing or self._creating[partition]
            adapter = self._adapters[partition]
            if not rejected and adapter is None:
                self._creating[partition] = True
        if rejected:
            raise AdmissionError("Partition closing or construction in progress")
        if adapter is not None:
            return adapter
        try:
            from necko.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_maxsize=16, pool_block=True)
            adapter._ruyijs_resources = self
            adapter._ruyijs_partition = partition
            adapter._ruyijs_resolver = self.resolver
            adapter._ruyijs_borrowed_resolver = True
            with self._lock:
                closing = self._closing
                if not closing:
                    self._adapters[partition] = adapter
            if closing:
                raise AdmissionError("Partition closed during construction")
            return adapter
        except BaseException:
            if adapter is not None:
                adapter.close()
            raise
        finally:
            with self._lock:
                self._creating[partition] = False
                self._lock.notify_all()

    def check(self, adapter, control):
        if control is None:
            raise ValueError("Worker transport requires RequestControl")
        control.remaining("connection-budget")
        with self._lock:
            if self._closing:
                raise AdmissionError("Transport resource owner closing")
            if self._adapters[adapter._ruyijs_partition] is not adapter:
                raise ValueError("Partition adapter identity")

    def acquire(self, adapter, protocol, control, attempt):
        self.check(adapter, control)
        if attempt is not None:
            attempt.remaining()
        return self.budget.acquire(adapter._ruyijs_partition, protocol)

    @property
    def status(self):
        present = [False] * 4
        with self._lock:
            closing = self._closing
            for index in range(4):
                present[index] = self._adapters[index] is not None
        return dict(partitions=tuple(i for i in range(4) if present[i]), closing=closing,
                    budget=self.budget.status, resolver=self.resolver.status)

    def close(self, timeout=2):
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError("Resource close timeout")
        deadline = time.monotonic() + timeout
        adapters = [None] * 4
        with self._lock:
            self._closing = True
            for index in range(4):
                adapters[index] = self._adapters[index]
        self.budget.stop()
        error = None
        for adapter in adapters:
            if adapter is None:
                continue
            try:
                adapter.close()
            except Exception as caught:
                error = error or caught
        try:
            self.resolver.close()
        except Exception as caught:
            error = error or caught
        try:
            self.budget.wait(deadline)
        except Exception as caught:
            error = error or caught
        with self._lock:
            while any(self._creating):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = error or TimeoutError("Partition construction remains owned")
                    break
                self._lock.wait(remaining)
        if error is not None:
            raise error


def reserve(adapter, protocol, control, attempt):
    owner = getattr(adapter, "_ruyijs_resources", None)
    return None if owner is None else owner.acquire(adapter, protocol, control, attempt)


def check_request(adapter, control):
    owner = getattr(adapter, "_ruyijs_resources", None)
    if owner is not None:
        owner.check(adapter, control)

"""One bounded remote-transport ledger; existing pools still own reuse."""
import threading
import time


class AdmissionError(Exception):
    phase = "connection-budget"


class ConnectionToken:
    def __init__(self, owner, partition, protocol):
        self._owner, self.partition, self.protocol = owner, partition, protocol
        self._state, self._claimed, self._signal = "reserved", False, None
        self._slot = None

    def claim(self):
        with self._owner._condition:
            if self._state == "released" or self._claimed:
                raise ValueError("Connection token ownership")
            self._claimed = True

    def bind(self, signal):
        with self._owner._condition:
            if self._state == "released":
                raise ValueError("Connection token released")
            previous = self._signal
            self._signal = signal
            closing = self._owner._closing
        if closing:
            signal()
        del previous

    def opened(self):
        with self._owner._condition:
            if self._owner._closing or self._state == "released":
                raise AdmissionError("Transport resource owner closing")
            self._state = "open"

    def closing(self):
        with self._owner._condition:
            if self._state != "released":
                self._state = "closing"

    def abandon(self):
        previous = None
        with self._owner._condition:
            if not self._claimed:
                previous = self._release()
        del previous

    def release(self):
        with self._owner._condition:
            previous = self._release()
        del previous

    def _release(self):
        if self._state == "released":
            return
        self._owner._slots[self._slot] = None
        self._owner._used -= 1
        previous = self._signal
        self._state, self._signal = "released", None
        self._owner._condition.notify_all()
        return previous


class ConnectionBudget:
    MAXIMUM = 16

    def __init__(self):
        self._condition = threading.Condition()
        self._slots, self._closing = [None] * self.MAXIMUM, False
        self._used, self._peak = 0, 0
        self._errors, self._error_count = [None] * 4, 0

    def acquire(self, partition, protocol):
        if type(partition) is not int or not 0 <= partition <= 3 or protocol not in ("tcp", "h3"):
            raise ValueError("Connection token identity")
        # Allocation/GC may reenter acquire; commit rechecks only fixed slots.
        token = ConnectionToken(self, partition, protocol)
        with self._condition:
            slot = 0
            while slot < self.MAXIMUM and self._slots[slot] is not None:
                slot += 1
            # A loop backedge can run pending GC. Recheck after the scan;
            # cached small integers and existing attributes make commit CALL-free.
            rejected = self._closing or self._used >= self.MAXIMUM or slot == self.MAXIMUM
            if not rejected:
                rejected = self._slots[slot] is not None
            if not rejected:
                used = self._used + 1
                peak = self._peak if self._peak >= used else used
                token._slot = slot
                self._slots[slot] = token
                self._used = used
                self._peak = peak
        if rejected:
            raise AdmissionError("Remote transport limit or owner closing")
        return token

    @property
    def status(self):
        errors = [None] * 4
        with self._condition:
            count, peak, closing = self._used, self._peak, self._closing
            reserved = opened = retiring = tcp = h3 = 0
            index = 0
            while index < self.MAXIMUM:
                token = self._slots[index]
                if token is not None:
                    reserved += token._state == "reserved"
                    opened += token._state == "open"
                    retiring += token._state == "closing"
                    tcp += token.protocol == "tcp"
                    h3 += token.protocol == "h3"
                index += 1
            for index in range(4):
                errors[index] = self._errors[index]
        return dict(connections=count, peak=peak, closing=closing, reserved=reserved,
                    open=opened, retiring=retiring, tcp=tcp, h3=h3,
                    errors=tuple(value for value in errors if value is not None))

    def stop(self):
        signals = [None] * self.MAXIMUM
        with self._condition:
            self._closing = True
            for index in range(self.MAXIMUM):
                token = self._slots[index]
                if token is not None:
                    token._state = "closing"
                    signals[index] = token._signal
        for signal in signals:
            if signal is None:
                continue
            try:
                signal()
            except Exception as error:
                diagnostic = (type(error).__name__, str(error)[:256])
                with self._condition:
                    if self._error_count < 4:
                        self._errors[self._error_count] = diagnostic
                        self._error_count += 1

    def wait(self, deadline):
        with self._condition:
            while self._used:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Remote transports remain owned")
                self._condition.wait(remaining)

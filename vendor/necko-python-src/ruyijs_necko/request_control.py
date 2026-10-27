"""Request deadline/cancellation and revocable interrupt registration."""
import math
import threading
import time


class Cancelled(Exception):
    pass


class DeadlineExceeded(Cancelled):
    pass


class AttemptLimit(Exception):
    pass


class _Registration:
    def __init__(self, owner, callback):
        self.owner, self.callback = owner, callback
        self._condition = threading.Condition()
        self._active, self._fired, self._running = True, False, None

    def fire(self):
        with self._condition:
            if not self._active or self._fired:
                return
            self._fired, self._running = True, threading.get_ident()
        try:
            self.callback()
        except Exception as error:
            self.owner._interrupt_error(error)
        finally:
            with self._condition:
                self._running = None
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._active = False
            while self._running is not None and self._running != threading.get_ident():
                self._condition.wait()
        self.owner._discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class RequestControl:
    """Interrupts signal only; owners dispose handles after registration.close()."""
    # Firefox network.http.request.max-attempts (modules/libpref/init/all.js).
    MAX_ATTEMPTS = 10

    def __init__(self, timeout, max_attempts=3):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or
                not 1 <= max_attempts <= self.MAX_ATTEMPTS):
            raise ValueError("max_attempts must be between 1 and %d" % self.MAX_ATTEMPTS)
        self.deadline = time.monotonic() + timeout
        self.max_attempts, self.attempts, self.phase = max_attempts, 0, "created"
        self._lock = threading.Lock()
        self._registrations, self._errors = set(), []
        self._exception = None
        self.cancelled = threading.Event()

    def _discard(self, registration):
        with self._lock:
            self._registrations.discard(registration)

    def _interrupt_error(self, error):
        with self._lock:
            if len(self._errors) < 4:
                self._errors.append((type(error).__name__, str(error)[:256]))

    @property
    def interrupt_errors(self):
        with self._lock:
            return tuple(self._errors)

    @property
    def registration_count(self):
        with self._lock:
            return len(self._registrations)

    def cancel(self, reason="Request cancelled", *, deadline=False):
        with self._lock:
            if self._exception is not None:
                return False
            kind = DeadlineExceeded if deadline else Cancelled
            self._exception = (kind, str(reason)[:512])
            self.cancelled.set()
            registrations = tuple(self._registrations)
        for registration in registrations:
            registration.fire()
        return True

    def register(self, interrupt):
        if not callable(interrupt):
            raise TypeError("interrupt must be callable")
        registration = _Registration(self, interrupt)
        with self._lock:
            if len(self._registrations) >= 32:
                raise ValueError("Interrupt registration limit exceeded")
            self._registrations.add(registration)
            cancelled = self._exception is not None
        if cancelled:
            registration.fire()
        elif time.monotonic() >= self.deadline:
            self.cancel("Request deadline elapsed", deadline=True)
        return registration

    def remaining(self, phase=None):
        if phase is not None:
            with self._lock:
                self.phase = str(phase)[:64]
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.cancel("Request deadline elapsed", deadline=True)
        with self._lock:
            error = self._exception
        if error:
            raise error[0](error[1])
        return remaining

    def begin_attempt(self):
        self.remaining("attempt")
        with self._lock:
            if self._exception:
                raise self._exception[0](self._exception[1])
            if self.attempts >= self.max_attempts:
                raise AttemptLimit("Request attempt budget exhausted")
            self.attempts += 1
            return self.attempts

    def child(self, deadline=None):
        child = RequestControl(self.remaining("child"), max_attempts=1)
        child.deadline = min(self.deadline, deadline if deadline is not None else self.deadline)
        return child

    def wait(self, event, *, phase="wait", timeout=None):
        end = self.deadline if timeout is None else min(self.deadline, time.monotonic() + timeout)
        while True:
            remaining = self.remaining(phase)
            if event.is_set():
                return True
            delay = min(remaining, end - time.monotonic(), .025)
            if delay <= 0:
                return False
            event.wait(delay)

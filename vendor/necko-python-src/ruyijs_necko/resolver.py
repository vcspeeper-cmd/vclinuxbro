"""Fixed concurrency DNS workers; cancellation kills only that query process."""
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from .launch import start_private


@dataclass(frozen=True)
class Address:
    family: int
    kind: int
    protocol: int
    sockaddr: tuple

    @property
    def numeric_host(self):
        suffix = f"%{self.sockaddr[3]}" if self.family == socket.AF_INET6 and self.sockaddr[3] else ""
        return self.sockaddr[0] + suffix


class Resolver:
    def __init__(self, maximum=2, queued=64):
        if not 1 <= maximum <= 2 or not 0 <= queued <= 64:
            raise ValueError("Resolver admission limit exceeded")
        self.maximum, self.queue_limit = maximum, queued
        self._condition = threading.Condition()
        self._active, self._queued, self._closed = {}, 0, False
        self._unreaped = {}
        self._program = Path(__file__).with_name("resolver_child.py")

    @property
    def status(self):
        with self._condition:
            return dict(active=len(self._active), queued=self._queued,
                        closed=self._closed, unreaped=len(self._unreaped),
                        pids=[p.pid for p in (*self._active.values(), *self._unreaped.values()) if p])

    def _acquire(self, control):
        control.remaining("dns-queue")
        with self._condition:
            if self._closed or self._unreaped:
                raise OSError("Resolver closed or has unreaped process")
            if control in self._active:
                raise ValueError("Control already resolving")
            if len(self._active) < self.maximum and not self._queued:
                self._active[control] = None
                return
            if self._queued >= self.queue_limit:
                raise ValueError("Resolver queue limit exceeded")
            self._queued += 1
        try:
            while True:
                remaining = control.remaining("dns-queue")
                with self._condition:
                    if self._closed:
                        raise OSError("Resolver closed")
                    if self._unreaped:
                        raise OSError("Resolver has unreaped process")
                    if control in self._active:
                        raise ValueError("Control already resolving")
                    if len(self._active) < self.maximum:
                        self._active[control] = None
                        return
                    self._condition.wait(min(remaining, .025))
        finally:
            with self._condition:
                self._queued -= 1
                self._condition.notify_all()

    def _spawn(self):
        return start_private(Path(sys.executable).parent.parent, self._program,
                             cwd=self._program.parent)

    def resolve(self, host, port, kind, control):
        if not isinstance(host, str) or not 1 <= len(host) <= 253 or "\0" in host:
            raise ValueError("Invalid DNS host")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Invalid DNS port")
        if kind not in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            raise ValueError("Invalid socket kind")
        self._acquire(control)
        process, registration = None, None
        try:
            control.remaining("dns")
            process = self._spawn()
            with self._condition:
                self._active[control] = process
            registration = control.register(lambda: process.kill() if process.poll() is None else None)
            payload = (json.dumps(dict(host=host, port=port, kind=int(kind))) + "\n").encode()
            while True:
                remaining = control.remaining("dns")
                try:
                    output, error = process.communicate(payload, timeout=min(.025, remaining))
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            control.remaining("dns")
            if process.returncode or len(output) > 16384 or error:
                raise OSError("Resolver worker failed")
            response = json.loads(output)
            if "error" in response:
                raise socket.gaierror(response["error"]["message"][:512])
            return self._addresses(response.get("addresses"), kind, port)
        finally:
            if registration:
                registration.close()
            try:
                if process:
                    self._reap(process)
            except Exception:
                with self._condition:
                    self._unreaped[control] = process
                raise
            finally:
                with self._condition:
                    self._active.pop(control, None)
                    self._condition.notify_all()

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()

    @staticmethod
    def _addresses(rows, kind, port):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
            raise OSError("Invalid resolver response")
        result = []
        for family, socktype, protocol, value in rows:
            if family not in (socket.AF_INET, socket.AF_INET6) or socktype != kind:
                raise OSError("Invalid address family")
            if len(value) != (2 if family == socket.AF_INET else 4) or value[1] != port:
                raise OSError("Invalid socket address")
            address = ipaddress.ip_address(value[0])
            if address.version != (4 if family == socket.AF_INET else 6):
                raise OSError("Address family mismatch")
            if family == socket.AF_INET6 and any(type(v) is not int or not 0 <= v <= 0xffffffff for v in value[2:]):
                raise OSError("Invalid IPv6 scope")
            result.append(Address(family, socktype, protocol, tuple(value)))
        return result

    def close(self):
        with self._condition:
            self._closed = True
            controls = tuple(self._active)
            self._condition.notify_all()
        for control in controls:
            control.cancel("Resolver closed")
        with self._condition:
            if not self._condition.wait_for(lambda: not self._active and not self._queued, timeout=2):
                raise TimeoutError("Resolver shutdown has outstanding owners")
            unreaped = tuple(self._unreaped.items())
        for control, process in unreaped:
            self._reap(process)
            with self._condition:
                self._unreaped.pop(control)

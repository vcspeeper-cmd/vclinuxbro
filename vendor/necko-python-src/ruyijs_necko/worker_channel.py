"""Worker-only client channel; caller stops and joins all users before close."""
import ctypes as ct
import threading
from . import worker_channel_win as win
from .worker_channel_io import operation

PREFIX = "\\\\.\\pipe\\ruyijs-necko-"
MAX_TRANSFER = 262144 + 4


class Channel:
    def __init__(self):
        self._lifetime = threading.Lock()
        self._read = threading.Lock()
        self._write = threading.Lock()
        self._stop = win.CreateEvent(None, True, False, None)
        if not self._stop:
            raise win.ChannelError("CreateStop")
        self._pipe = None
        self._initialized = self._closed = self._ready = False

    def stop(self):
        with self._lifetime:
            if self._stop:
                win.SetEvent(self._stop)

    def connect(self, name, expected_pid, deadline_ms):
        win.deadline(deadline_ms)
        if (type(name) is not str or not name.startswith(PREFIX) or len(name) != len(PREFIX) + 32 or
                any(c not in "0123456789abcdef" for c in name[len(PREFIX):]) or
                type(expected_pid) is not int or not 1 <= expected_pid <= 4294967295):
            raise ValueError("Invalid worker pipe identity")
        if not self._read.acquire(False):
            raise win.ChannelError("ConnectBusy", 170)
        try:
            with self._lifetime:
                if self._closed or self._initialized:
                    raise win.ChannelError("ConnectState", 5023)
                self._initialized = True
            while True:
                if win.WaitOne(self._stop, 0) == 0:
                    raise win.ChannelError("ConnectStopped", win.ERROR_ABORTED)
                now = win.ticks()
                if now >= deadline_ms:
                    raise win.ChannelError("ConnectDeadline", win.WAIT_TIMEOUT)
                pipe = win.CreateFile(name, 0xC0000000, 0, None, 3, 0x40000000, None)
                if pipe != win.INVALID_HANDLE:
                    try:
                        peer = win.DWORD()
                        if not win.ServerPid(pipe, ct.byref(peer)):
                            raise win.ChannelError("ServerPid")
                        if peer.value != expected_pid:
                            raise win.ChannelError("ServerPid", 5)
                        if not win.SetHandleInfo(pipe, 1, 0):
                            raise win.ChannelError("NoninheritHandle")
                        if win.WaitOne(self._stop, 0) == 0:
                            raise win.ChannelError("ConnectStopped", win.ERROR_ABORTED)
                        if win.ticks() >= deadline_ms:
                            raise win.ChannelError("ConnectDeadline", win.WAIT_TIMEOUT)
                        with self._lifetime:
                            self._pipe, self._ready = pipe, True
                        pipe = None
                        return
                    finally:
                        if pipe is not None:
                            win.CloseHandle(pipe)
                code = ct.get_last_error()
                if code not in (2, 231):
                    raise win.ChannelError("CreateFile", code)
                remaining = deadline_ms - win.ticks()
                if remaining <= 0:
                    raise win.ChannelError("ConnectDeadline", win.WAIT_TIMEOUT)
                delay = min(remaining, 25)
                if code == 231:
                    win.WaitPipe(name, delay)
                elif win.WaitOne(self._stop, delay) == 0:
                    raise win.ChannelError("ConnectStopped", win.ERROR_ABORTED)
        except BaseException:
            self.stop()
            raise
        finally:
            self._read.release()

    def _transfer(self, writing, value, deadline_ms, stall_ms=0):
        if not stall_ms:
            win.deadline(deadline_ms)
        size = len(value) if writing and type(value) is bytes else value if not writing else 0
        if type(size) is not int or not 1 <= size <= MAX_TRANSFER:
            raise ValueError("Invalid worker exact transfer size/type")
        gate = self._write if writing else self._read
        if not gate.acquire(False):
            raise win.ChannelError("TransferBusy", 170)
        try:
            with self._lifetime:
                if self._closed or not self._ready:
                    raise win.ChannelError("TransferState", 5023)
            buffer = (ct.c_ubyte * size).from_buffer_copy(value) if writing else (ct.c_ubyte * size)()
            offset = 0
            while offset < size:
                idle = bool(stall_ms and not offset)
                offset += operation(self._pipe, self._stop, writing, ct.byref(buffer, offset),
                                    1 if idle else min(65536, size - offset), deadline_ms, idle=idle)
                if idle:
                    deadline_ms = win.deadline(win.ticks() + stall_ms)
            if stall_ms:
                return bytes(buffer), deadline_ms
            return None if writing else bytes(buffer)
        except BaseException:
            self.stop()
            raise
        finally:
            gate.release()

    def read_exact(self, size, deadline_ms):
        return self._transfer(False, size, deadline_ms)

    def read_start(self, size, stall_ms=30000):
        if type(size) is not int or not 1 <= size <= 52:
            raise ValueError("Invalid frame prefix size")
        win.stall(stall_ms)
        return self._transfer(False, size, None, stall_ms)

    def write_exact(self, data, deadline_ms):
        return self._transfer(True, data, deadline_ms)

    def close(self):
        self.stop()
        if not self._read.acquire(False):
            raise win.ChannelError("CloseRequiresJoin", 170)
        try:
            if not self._write.acquire(False):
                raise win.ChannelError("CloseRequiresJoin", 170)
            try:
                with self._lifetime:
                    if not self._closed:
                        if self._pipe:
                            win.CloseHandle(self._pipe)
                        win.CloseHandle(self._stop)
                        self._pipe = self._stop = None
                        self._ready, self._closed = False, True
            finally:
                self._write.release()
        finally:
            self._read.release()

"""One kernel operation; buffers/events survive until cancellation is acknowledged."""
import ctypes as ct
from . import worker_channel_win as win


def operation(pipe, stop, writing, address, size, deadline, *, idle=False):
    if idle and (writing or size != 1):
        raise ValueError("Invalid idle operation")
    if win.WaitOne(stop, 0) == 0:
        raise win.ChannelError("Stopped", win.ERROR_ABORTED)
    if not idle and win.ticks() >= deadline:
        raise win.ChannelError("Deadline", win.WAIT_TIMEOUT)
    overlapped = win.Overlapped()
    overlapped.hEvent = win.CreateEvent(None, True, False, None)
    if not overlapped.hEvent:
        raise win.ChannelError("CreateIoEvent")
    count, pending = win.DWORD(), False
    try:
        function = win.WriteFile if writing else win.ReadFile
        ok = function(pipe, address, size, ct.byref(count), ct.byref(overlapped))
        code = 0 if ok else ct.get_last_error()
        if code == win.ERROR_PENDING:
            pending = True
            now = win.ticks()
            timeout = win.INFINITE if idle else max(0, min(deadline - now, win.INFINITE - 1))
            events = (win.HANDLE * 2)(stop, overlapped.hEvent)
            waited = win.WaitMany(2, events, False, timeout)
            if waited != 1:
                code = win.ERROR_ABORTED if waited == 0 else win.WAIT_TIMEOUT if waited == win.WAIT_TIMEOUT else ct.get_last_error()
                raise win.ChannelError("WriteWait" if writing else "ReadWait", code)
            ok = win.GetResult(pipe, ct.byref(overlapped), ct.byref(count), False)
            code = 0 if ok else ct.get_last_error()
            pending = code == 996  # ERROR_IO_INCOMPLETE still owns the buffer.
        if code:
            raise win.ChannelError("WriteFile" if writing else "ReadFile", code)
        if not 1 <= count.value <= size:
            raise win.ChannelError("TransferEOF", 109)
        return count.value
    finally:
        if pending:
            win.CancelIo(pipe, ct.byref(overlapped))
            win.GetResult(pipe, ct.byref(overlapped), ct.byref(count), True)
        win.CloseHandle(overlapped.hEvent)

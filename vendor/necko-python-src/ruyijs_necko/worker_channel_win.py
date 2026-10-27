"""Explicit Windows ABI for worker overlapped pipe client; bounded static errors."""
import ctypes as ct
from ctypes import wintypes as wt

KERNEL = ct.WinDLL("kernel32", use_last_error=True)
HANDLE = wt.HANDLE
DWORD = wt.DWORD
INVALID_HANDLE = ct.c_void_p(-1).value
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 258
ERROR_ABORTED = 995
ERROR_PENDING = 997


class _Offsets(ct.Structure):
    _fields_ = [("Offset", DWORD), ("OffsetHigh", DWORD)]


class _Position(ct.Union):
    _fields_ = [("offsets", _Offsets), ("Pointer", ct.c_void_p)]


class Overlapped(ct.Structure):
    _fields_ = [("Internal", ct.c_size_t), ("InternalHigh", ct.c_size_t),
                ("position", _Position), ("hEvent", HANDLE)]


def _declare(name, restype, arguments):
    function = getattr(KERNEL, name)
    function.restype, function.argtypes = restype, arguments
    return function


CreateEvent = _declare("CreateEventW", HANDLE, [ct.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR])
SetEvent = _declare("SetEvent", wt.BOOL, [HANDLE])
CloseHandle = _declare("CloseHandle", wt.BOOL, [HANDLE])
WaitOne = _declare("WaitForSingleObject", DWORD, [HANDLE, DWORD])
WaitMany = _declare("WaitForMultipleObjects", DWORD, [DWORD, ct.POINTER(HANDLE), wt.BOOL, DWORD])
CreateFile = _declare("CreateFileW", HANDLE, [wt.LPCWSTR, DWORD, DWORD, ct.c_void_p, DWORD, DWORD, HANDLE])
WaitPipe = _declare("WaitNamedPipeW", wt.BOOL, [wt.LPCWSTR, DWORD])
ServerPid = _declare("GetNamedPipeServerProcessId", wt.BOOL, [HANDLE, ct.POINTER(wt.ULONG)])
SetHandleInfo = _declare("SetHandleInformation", wt.BOOL, [HANDLE, DWORD, DWORD])
ReadFile = _declare("ReadFile", wt.BOOL, [HANDLE, ct.c_void_p, DWORD, ct.POINTER(DWORD), ct.POINTER(Overlapped)])
WriteFile = _declare("WriteFile", wt.BOOL, [HANDLE, ct.c_void_p, DWORD, ct.POINTER(DWORD), ct.POINTER(Overlapped)])
CancelIo = _declare("CancelIoEx", wt.BOOL, [HANDLE, ct.POINTER(Overlapped)])
GetResult = _declare("GetOverlappedResult", wt.BOOL, [HANDLE, ct.POINTER(Overlapped), ct.POINTER(DWORD), wt.BOOL])
ticks = _declare("GetTickCount64", ct.c_uint64, [])
process_id = _declare("GetCurrentProcessId", DWORD, [])
_frequency = _declare("QueryPerformanceFrequency", wt.BOOL, [ct.POINTER(ct.c_int64)])


class ChannelError(OSError):
    def __init__(self, operation, code=None):
        self.operation = operation
        actual = ct.get_last_error() if code is None else code
        super().__init__(actual, "Worker channel " + operation)
        self.winerror = actual


def frequency():
    value = ct.c_int64()
    if not _frequency(ct.byref(value)) or value.value <= 0:
        raise ChannelError("QpcFrequency")
    return value.value


def deadline(value):
    if type(value) is not int or not 1 <= value < (1 << 64) - 1:
        raise ValueError("Invalid channel absolute deadline")
    return value


def stall(value):
    if type(value) is not int or not 1 <= value <= 60000:
        raise ValueError("Invalid frame stall timeout")
    return value

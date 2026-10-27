"""One persistent SpiderMonkey process per Python Context."""

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from collections import deque

from ._pipe import MAX_FRAME, Pipe
from ._callbacks import Callbacks
from ._environment import EnvironmentControl
from ._scheduling import SchedulingControl
from ._random import RandomControl
from ._resources import ResourceControl
from ._console import ConsoleControl
from ._inspector import InspectorControl
from ._input import InputControl
from ._context_eval import EvaluationControl
from ._runtime_paths import get_runtime_path
from ._values import JSError, decode, encode


class Context(EnvironmentControl, SchedulingControl, RandomControl, ResourceControl, ConsoleControl,
              InspectorControl, InputControl, EvaluationControl):
    """Evaluate JavaScript from Python 3.10+ with bounded transport waits.

    ``timeout`` applies to startup and each request after it acquires the context
    lock. A timeout or broken protocol closes the context and kills its process.
    ``shell`` overrides RUYIJS_SHELL; otherwise select the source or bundled runtime.
    ``proxy`` selects an explicit HTTP(S) proxy URL for this context's network
    requests. None or an empty string uses direct connections. It does not
    alter the host environment or other contexts.
    """

    def __init__(self, shell=None, timeout=10.0, environment=None, mode="default", proxy=None):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.timeout = float(timeout)
        if mode not in ("default", "debug"):
            raise ValueError("mode must be 'default' or 'debug'")
        if proxy is not None and not isinstance(proxy, str):
            raise TypeError("proxy must be a URL string or None")
        if proxy and any(c in proxy for c in "\x00\r\n"):
            raise ValueError("proxy URL contains invalid characters")
        # Capture transport settings for this child without changing os.environ.
        child_environment = dict(os.environ)
        child_environment["RUYIJS_HTTP_PROXY"] = proxy or ""
        self.mode = mode
        self._lock, self._next_id = threading.Lock(), 0
        self._callbacks = Callbacks()
        self._host_releases = deque()
        self._host_callback_sequence = 0
        self._closed, self._pipe, self._process, self._stderr = True, None, None, None
        selected = shell or os.environ.get("RUYIJS_SHELL")
        self.shell = (Path(selected).resolve() if selected else
                      get_runtime_path() / "dist" / "js.exe")
        if not self.shell.is_file():
            raise FileNotFoundError(f"ruyijs shell does not exist: {self.shell}")
        try:
            self._pipe = Pipe()
            self._stderr = tempfile.TemporaryFile()
            self._process = subprocess.Popen(
                [str(self.shell), "--ruyijs-rpc", self._pipe.name,
                 "--ruyijs-mode", mode],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._stderr,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
                env=child_environment,
            )
            self._pipe.connect(time.monotonic() + self.timeout)
            self._closed = False
            if environment is not None:
                self.configure_environment(environment)
        except BaseException as error:
            if isinstance(error, EOFError) or (
                    isinstance(error, OSError) and error.winerror == 109):
                if self._process is not None and self._process.poll() is None:
                    try:
                        self._process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                exit_code = self._process.returncode if self._process is not None else None
                stderr_tail = ""
                if self._stderr is not None:
                    try:
                        self._stderr.seek(0)
                        stderr_tail = self._stderr.read(8192).decode("utf-8", "replace")
                    except OSError:
                        pass
                detail = f"ruyijs process exited (code {exit_code})"
                if stderr_tail:
                    detail += ": " + stderr_tail[-2000:]
                self._dispose()
                raise BrokenPipeError(detail) from error
            self._dispose()
            raise

    @property
    def closed(self):
        return self._closed

    def _dispose(self):
        self._closed = True
        if hasattr(self, "_host_releases"):
            self._host_releases.clear()
        if hasattr(self, "_callbacks"):
            self._callbacks.functions.clear()
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)
        if self._pipe is not None:
            self._pipe.close()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def _request(self, operation, **fields):
        if self._closed:
            raise RuntimeError("ruyijs Context is closed")
        self._next_id += 1
        request = {"id": self._next_id, "op": operation, **fields}
        payload = json.dumps(request, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_FRAME:
            raise ValueError("ruyijs request exceeds 16 MiB")
        deadline = time.monotonic() + self.timeout
        try:
            self._pipe.send(payload, deadline)
            response = json.loads(self._pipe.receive(deadline).decode("utf-8"))
            while isinstance(response, dict) and response.get("event") == "callback":
                result = self._callbacks.dispatch(response, deadline)
                payload = json.dumps(result, ensure_ascii=True, allow_nan=False).encode("utf-8")
                self._pipe.send(payload, deadline)
                response = json.loads(self._pipe.receive(deadline).decode("utf-8"))
            if not isinstance(response, dict) or type(response.get("id")) is not int:
                raise ValueError("invalid ruyijs response identity")
            if response["id"] != self._next_id or type(response.get("ok")) is not bool:
                raise ValueError("mismatched ruyijs response")
            if response["ok"]:
                return decode(response.get("value"))
            error = response.get("error")
            if not isinstance(error, dict) or any(
                not isinstance(error.get(key), str) for key in ("name", "message", "stack")
            ):
                raise ValueError("invalid ruyijs error response")
            raise JSError(error["name"], error["message"], error["stack"])
        except JSError:
            raise
        except BaseException:
            self._dispose()
            raise

    def load_page(self, url, html, *, replay_async_scripts=None):
        """Navigate the page to url and load html into the main document.

        Parser-inserted classic scripts run in order with document.currentScript
        set; src scripts are read through the resource directory or network.
        readyState then becomes interactive (DOMContentLoaded) and complete
        (load). The first uncaught script or listener error is raised after the
        load finishes. Returns {"url", "scripts"}.

        replay_async_scripts optionally lists recorded async classic URLs to
        run at the interactive boundary, draining due tasks between entries.
        This explicit replay phase does not reproduce a complete task schedule.
        """
        if not isinstance(url, str) or not isinstance(html, str):
            raise TypeError("url and html must be strings")
        self._callbacks.guard()
        with self._lock:
            fields = {} if replay_async_scripts is None else {"replay_async_scripts": replay_async_scripts}
            return self._request("page", action="load", url=url, html=html, **fields)

    def navigate(self, url):
        """Start a top-level navigation the browser itself makes (address bar).

        The request has no Referer, Sec-Fetch-Site "none" and Sec-Fetch-User
        "?1"; a later location.reload() of that document is made the same way.
        The navigation runs on the event loop: advance() lets it fetch and
        commit. Returns {"url"}.
        """
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        self._callbacks.guard()
        with self._lock:
            return self._request("page", action="navigate", url=url)

    def checkpoint(self):
        """Drain pending engine jobs through the shared runtime coordinator."""
        self._callbacks.guard()
        with self._lock:
            return self._request("checkpoint")

    def collect_garbage(self):
        """Run a full engine collection and return the major GC counter."""
        self._callbacks.guard()
        with self._lock:
            self._flush_host_releases()
            return self._request("collect")

    def expose(self, function, name=None):
        """Expose a global host function, or copy data into __ruyijs__.data[name].

        Data needs an explicit name and adds no Window property. The lexical
        __ruyijs__ namespace remains accessible to scripts that know its name.
        """
        if not callable(function):
            if not isinstance(name, str) or not name or "\0" in name:
                raise ValueError("exposed data requires a nonempty name without NUL")
            self._callbacks.guard()
            with self._lock:
                self._request("expose_data", name=name, value=encode(function))
            return
        name = name if name is not None else getattr(function, "__name__", None)
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("host function requires a nonempty name without NUL")
        self._callbacks.guard()
        with self._lock:
            self._request("expose", name=name)
            self._callbacks.functions[name] = function

    def set(self, name, value):
        """Copy a Python value to a property of this context's page global."""
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("global property requires a nonempty name without NUL")
        self._callbacks.guard()
        with self._lock:
            self._request("set", name=name, value=encode(value))

    def native(self, name, source, *, constructor=False):
        """Bind a native host wrapper, optionally callable with new."""
        if not isinstance(name, str) or "\0" in name:
            raise ValueError("native function requires a string name without NUL")
        if not isinstance(source, str):
            raise TypeError("native source must be a string")
        if not isinstance(constructor, bool):
            raise TypeError("native constructor must be a boolean")
        self._callbacks.guard()
        with self._lock:
            self._request("native", name=name, code=source, constructor=constructor)

    def close(self):
        """Close the shell process; repeated close calls have no effect."""
        if not self._closed:
            self._callbacks.guard()
        with self._lock:
            if self._closed:
                return
            try:
                self._request("close")
                self._process.wait(timeout=self.timeout)
            finally:
                self._dispose()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("ruyijs Context is closed")
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self._dispose()
        except Exception:
            pass

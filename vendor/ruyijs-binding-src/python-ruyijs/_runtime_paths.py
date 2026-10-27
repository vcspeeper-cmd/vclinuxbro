"""Locate the Windows x64 runtime for Python 3.10+ clients."""

import os
from pathlib import Path
import platform


def get_runtime_path():
    """Return the runtime root containing ``dist`` and ``vendor``.

    ``RUYIJS_SHELL`` selects an existing executable and its enclosing runtime
    root. Otherwise prefer a source checkout's ``dist/js.exe`` to the runtime
    bundled in the installed package. This function never downloads files.
    """
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise OSError("ruyijs requires Windows x64")

    override = os.environ.get("RUYIJS_SHELL")
    if override:
        shell = Path(override).resolve()
        if not shell.is_file():
            raise FileNotFoundError(f"RUYIJS_SHELL does not exist: {shell}")
        return shell.parent.parent

    package = Path(__file__).resolve().parent
    candidates = [package / "_runtime"]
    if package.parent.name == "python":
        candidates.insert(0, package.parent.parent)
    for root in candidates:
        if (root / "dist" / "js.exe").is_file():
            return root

    searched = ", ".join(str(root / "dist" / "js.exe") for root in candidates)
    raise FileNotFoundError(
        "ruyijs runtime is missing. Install the Windows x64 wheel, build the "
        "source runtime, or set RUYIJS_SHELL to an existing executable. "
        f"Searched: {searched}"
    )

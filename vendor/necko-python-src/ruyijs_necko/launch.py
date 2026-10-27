"""Environment policy shared by private transport/resolver subprocess launchers."""
import os
from pathlib import Path
import subprocess


OVERRIDES = ("NECKO_CORE_LIBRARY", "NECKO_QUIC_LIBRARY", "PYTHONPATH", "PYTHONHOME",
             "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV")


def private_environment(source=None):
    environment = dict(os.environ if source is None else source)
    for key in tuple(environment):
        if key.upper() in OVERRIDES:
            environment.pop(key)
    return environment


def start_private(runtime, program, arguments=(), *, cwd=None, environment=None):
    runtime = Path(runtime).resolve()
    executable = runtime / "python/python.exe"
    program = Path(program).resolve()
    if not executable.is_file() or not program.is_file():
        raise ValueError("Private executable or entry point missing")
    return subprocess.Popen([str(executable), "-I", "-B", str(program), *map(str, arguments)],
        cwd=str(Path(cwd).resolve() if cwd else runtime), env=private_environment(environment),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

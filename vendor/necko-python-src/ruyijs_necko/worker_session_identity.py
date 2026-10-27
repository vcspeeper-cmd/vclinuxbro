"""Worker process-local identity from the fixed private runtime layout."""
import hashlib
from pathlib import Path
from . import worker_channel_win as win
from .worker_schema_types import hexadecimal


def local_identity(expected_manifest):
    hexadecimal(64)(expected_manifest)
    module = Path(__file__).resolve()
    package = module.parent
    if (package.name != "ruyijs_necko" or package.parent.name != "site-packages" or
            package.parent.parent.name != "Lib" or package.parent.parent.parent.name != "python"):
        raise ValueError("Worker requires fixed private runtime layout")
    root = module.parents[4]
    digest = hashlib.sha256()
    total = 0
    with (root / "manifest.json").open("rb") as source:
        while block := source.read(65536):
            total += len(block)
            if total > 4 * 1024 * 1024:
                raise ValueError("Worker manifest exceeds startup limit")
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected_manifest:
        raise ValueError("Worker private manifest identity differs")
    return dict(pid=int(win.process_id()), manifest=actual, qpcFrequency=str(win.frequency()))

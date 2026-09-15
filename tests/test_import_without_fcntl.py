"""The POSIX-only ``fcntl`` import must not be fatal where ``fcntl`` does not exist.

``extract_bench_v3`` and ``operator_receipts_v3`` are imported at module scope by
``search.py`` and ``orchestrator_v3.py``, so a bare ``import fcntl`` in either one takes the whole
plugin down on Windows (there is no ``fcntl`` module there): the search engine raises
``ModuleNotFoundError`` instead of loading, and every search/extract call fails.

Their lock/journal paths are already POSIX-only by construction — ``os.O_CLOEXEC``,
``dir_fd``, ``os.geteuid`` all fail earlier than the ``flock`` call on a non-POSIX platform — and
every caller treats that subsystem as best-effort (``orchestrator_v3`` wraps the journal append in
``except Exception``). The import therefore has to degrade, not raise.
"""

import builtins
import importlib
import sys

import pytest

POSIX_ONLY_LOCK_MODULES = ("extract_bench_v3", "operator_receipts_v3")


@pytest.fixture
def without_fcntl(monkeypatch):
    """Resolve ``import fcntl`` the way Windows does: ImportError."""
    real_import = builtins.__import__

    def windows_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", windows_import)
    for name in POSIX_ONLY_LOCK_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield
    # Never leave a fcntl-less copy behind for the rest of the session: the next import of these
    # modules must read the real module. (monkeypatch then restores any pre-existing entry.)
    for name in POSIX_ONLY_LOCK_MODULES:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("module_name", POSIX_ONLY_LOCK_MODULES)
def test_posix_only_lock_module_imports_without_fcntl(module_name, without_fcntl):
    module = importlib.import_module(module_name)

    assert module.fcntl is None

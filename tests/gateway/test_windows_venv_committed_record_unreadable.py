"""An unreadable committed-environment record must not stop gateway boot (#122183 residual).

``committed_venv()`` raises ``RuntimeError`` for a corrupt ``facts.json``, an invalid
record, or a generation that is missing / outside this install. ``cron``'s worker env
already degrades on exactly that; the gateway called it unguarded, so one bad record
turned "gateway must start" into a traceback out of ``start_gateway`` — the same
no-start class #122183 was filed under.

The legacy overlay stays ABI-guarded (#139085), so degrading into it is safe: the
gateway boots and still refuses to load a foreign-ABI venv.

The platform is stubbed rather than marked ``platforms("windows")`` so the guard runs on
every host -- the defect is in the call, not in the Windows layout.
"""

import logging
import os
import sys
import types

import pytest


def _win_sys():
    """A ``sys`` stand-in that reports win32, as the gateway sees it."""
    return types.SimpleNamespace(platform="win32", version_info=sys.version_info, path=list(sys.path))


def _venv(path, version):
    (path / "Lib" / "site-packages").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text(f"home = C:\\Python\nversion_info = {version}\n", encoding="utf-8")
    return path


def _raises_boom(_root):
    raise RuntimeError("cannot read dependency environment: facts.json")


def test_unreadable_committed_record_does_not_stop_gateway_boot(tmp_path, monkeypatch, caplog):
    """A corrupt record falls through to the ABI-guarded legacy overlay, and says so."""
    import gateway.run as gateway_run

    root = tmp_path / "root"
    (root / "gateway").mkdir(parents=True)
    same_abi = _venv(root / "venv", "{}.{}.{}".format(*sys.version_info[:3]))

    monkeypatch.setattr("pm.environments.committed_venv", _raises_boom)
    monkeypatch.setattr(gateway_run, "__file__", str(root / "gateway" / "run.py"))
    monkeypatch.setattr(gateway_run, "sys", _win_sys())
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        gateway_run._ensure_windows_gateway_venv_imports()  # must not raise

    assert "committed dependency environment" in caplog.text
    # Degraded, not abandoned: the same-ABI legacy venv is still overlaid.
    assert str(same_abi / "Lib" / "site-packages") in gateway_run.sys.path
    assert os.environ["VIRTUAL_ENV"] == str(same_abi.resolve())


def test_unreadable_committed_record_still_fails_closed_on_foreign_abi(tmp_path, monkeypatch, caplog):
    """Degrading into the overlay must not re-open the #139085 foreign-ABI hole."""
    import gateway.run as gateway_run

    root = tmp_path / "root"
    (root / "gateway").mkdir(parents=True)
    foreign_minor = 11 if sys.version_info.minor != 11 else 12
    foreign = _venv(root / "venv", f"{sys.version_info.major}.{foreign_minor}.9")

    monkeypatch.setattr("pm.environments.committed_venv", _raises_boom)
    monkeypatch.setattr(gateway_run, "__file__", str(root / "gateway" / "run.py"))
    monkeypatch.setattr(gateway_run, "sys", _win_sys())
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    # DEBUG: the ABI skip is logged there, the way the #139085 guard does.
    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        gateway_run._ensure_windows_gateway_venv_imports()

    site_packages = str(foreign / "Lib" / "site-packages")
    assert site_packages not in gateway_run.sys.path
    assert "VIRTUAL_ENV" not in os.environ
    assert "Skipping venv overlay" in caplog.text


def test_unreadable_record_is_not_widened_to_silence_real_errors(tmp_path, monkeypatch):
    """Only the record lookup is guarded -- a later failure in the overlay still propagates."""
    import gateway.run as gateway_run

    root = tmp_path / "root"
    (root / "gateway").mkdir(parents=True)
    _venv(root / "venv", "{}.{}.{}".format(*sys.version_info[:3]))

    def explode(_venv_dir):
        raise ValueError("overlay blew up")

    monkeypatch.setattr("pm.environments.committed_venv", _raises_boom)
    monkeypatch.setattr(gateway_run, "__file__", str(root / "gateway" / "run.py"))
    monkeypatch.setattr(gateway_run, "sys", _win_sys())
    monkeypatch.setattr("pm.environments.venv_python_version", explode)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    with pytest.raises(ValueError, match="overlay blew up"):
        gateway_run._ensure_windows_gateway_venv_imports()

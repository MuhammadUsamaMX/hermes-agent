"""Regression tests for scanner-safe GitHub credential extraction (#22722)."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "skills/software-development/github/scripts/git-credential-token.py"


def _extract(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _credential_file(tmp_path: Path, value: str) -> Path:
    credentials = tmp_path / "credentials"
    credentials.write_text(value, encoding="utf-8", newline="")
    return credentials


@pytest.mark.parametrize(
    ("credential", "token"),
    [
        ("https://octocat:password-form-token@github.com\n", "password-form-token"),
        ("https://oauth-token:x-oauth-basic@github.com\n", "oauth-token"),
        ("https://ghp_token_only@github.com\n", "ghp_token_only"),
        ("https://github_pat_token_only@github.com\n", "github_pat_token_only"),
    ],
)
def test_extracts_supported_git_credential_url_forms(tmp_path, credential, token):
    result = _extract(_credential_file(tmp_path, credential))

    assert result.returncode == 0
    assert result.stdout == f"{token}\n"
    assert result.stderr == ""


def test_extracts_password_from_exact_github_https_credential(tmp_path):
    credentials = _credential_file(
        tmp_path,
        "https://ignored:wrong@example.com\n"
        "https://octocat:secret%2Ftoken@github.com\n",
    )

    result = _extract(credentials)

    assert result.returncode == 0
    assert result.stdout == "secret/token\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "credential",
    [
        "https://octocat:stolen@github.com.attacker.example\n",
        "https://octocat@github.com\n",
        "https://%6fctocat@github.com\n",
        "https://octocat:token@github.com%2eattacker.example\n",
        "https://octocat:token%0D%0AX-Injected%3Ayes@github.com\n",
        "https://ghp_token%0Ainjected@github.com\n",
        "https://octocat:token%00suffix@github.com\n",
        "https://octocat:token%09suffix@github.com\n",
        "https://octocat:token%C2%85suffix@github.com\n",
        "https://ghp_token%1Fsuffix@github.com\n",
        "https://ghp_token%C2%9Fsuffix@github.com\n",
        "https://octocat:bad%ZZtoken@github.com\n",
        "https://octocat:token@github.com:bogus\n",
        "http://octocat:token@github.com\n",
    ],
)
def test_rejects_ambiguous_lookalike_or_malformed_credentials(tmp_path, credential):
    result = _extract(_credential_file(tmp_path, credential))

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""


def test_helper_defers_annotation_evaluation_so_it_imports_on_python_39():
    """PEP 604 unions must not be evaluated at import time.

    The helper is documented as a standalone ``python3 <path>`` invocation, so it
    has to import on interpreters older than the ``requires-python`` floor the
    package itself declares. Without ``from __future__ import annotations`` the
    ``str | None`` annotation on ``_decode`` is evaluated eagerly and Python 3.9
    raises ``TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'``
    at import.

    Asserting the annotations are *strings* pins the actual mechanism, so the
    test bites on every interpreter (including CI's own) rather than only on a
    real 3.9 runtime. See #125202.
    """
    spec = importlib.util.spec_from_file_location("git_credential_token", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    annotations = module._decode.__annotations__

    assert annotations, "expected _decode to carry annotations"
    non_postponed = {
        name: value for name, value in annotations.items() if not isinstance(value, str)
    }
    assert non_postponed == {}, (
        "helper annotations are evaluated at import time, so PEP 604 unions crash "
        "on Python 3.9; add 'from __future__ import annotations' (#125202)"
    )

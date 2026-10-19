"""Behavior tests for the security evidence ledger gate (scripts/ci/security_evidence_gate.py).

The ledger (security/evidence-ledger.json) records, per control, the exact object it was
observed against and how it was verified; the gate is what makes those rows mean something:
a required control whose evidence is failed, unknown, missing, stale, or unverifiable blocks
an unqualified pass (#92618 acceptance criterion 7). `unknown` stays first-class — it is never
silently promoted to pass, and an accepted-risk exception is recorded beside the original
result rather than rewriting it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci" / "security_evidence_gate.py"
LEDGER = REPO / "security" / "evidence-ledger.json"

import importlib.util

_spec = importlib.util.spec_from_file_location("security_evidence_gate", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── ledger fixtures ────────────────────────────────────────────────────────────

def _control(**over):
    base = {
        "id": "SCA-TEST-01",
        "surface": "dependency supply chain",
        "claim": "Weekly OSV advisory scan runs against main",
        "standard": "SLSA v1.2",
        "verification": {"method": "repo_file", "path": "SECURITY.md"},
        "result": "pass",
        "required": True,
        "owner": "maintainers",
        "observed": "2026-10-01",
        "expires": "2027-01-01",
        "scope": "linux x86_64 source checkout",
        "evidence": [{"kind": "workflow", "receipt": "osv-scanner.yml", "observed": "2026-10-01"}],
        "exception": None,
    }
    base.update(over)
    return base


def _ledger(*controls, **over):
    doc = {"schema_version": 1, "controls": list(controls)}
    doc.update(over)
    return doc


AS_OF = date(2026, 10, 19)


def _evaluate(doc, as_of=AS_OF, root=REPO):
    return gate.evaluate(doc, root=root, as_of=as_of)


# ── schema validation: a malformed ledger never passes ─────────────────────────

def test_duplicate_control_ids_block():
    verdict = _evaluate(_ledger(_control(), _control()))
    assert not verdict["ok"]
    assert any("duplicate" in r for r in verdict["blocking"])


def test_missing_required_field_blocks():
    broken = _control()
    del broken["owner"]
    verdict = _evaluate(_ledger(broken))
    assert not verdict["ok"]
    assert any("owner" in r for r in verdict["blocking"])


def test_unknown_result_value_blocks():
    verdict = _evaluate(_ledger(_control(result="looks-fine")))
    assert not verdict["ok"]


def test_not_applicable_requires_a_justification():
    verdict = _evaluate(_ledger(_control(result="not_applicable", justification=None)))
    assert not verdict["ok"]
    verdict = _evaluate(_ledger(_control(result="not_applicable",
                                          justification="desktop updater never enabled")))
    assert verdict["ok"]


# ── unknown and fail are first-class, blocking states ─────────────────────────

@pytest.mark.parametrize("result", ["unknown", "fail"])
def test_required_unknown_or_fail_blocks(result):
    verdict = _evaluate(_ledger(_control(result=result)))
    assert not verdict["ok"]
    assert verdict["blocked_ids"] == ["SCA-TEST-01"]


def test_optional_unknown_is_reported_but_does_not_block():
    verdict = _evaluate(_ledger(_control(result="unknown", required=False)))
    assert verdict["ok"]
    assert verdict["blocked_ids"] == []
    assert "SCA-TEST-01" in verdict["non_blocking_unknown"]


# ── missing / stale evidence ──────────────────────────────────────────────────

def test_required_recorded_control_without_evidence_is_missing():
    verdict = _evaluate(_ledger(_control(evidence=[], verification={"method": "recorded"})))
    assert not verdict["ok"]
    assert verdict["blocked_ids"] == ["SCA-TEST-01"]


def test_stale_evidence_blocks_before_it_expires_not_after():
    control = _control(expires="2026-12-01")
    assert _evaluate(_ledger(control), as_of=date(2026, 11, 30))["ok"]
    verdict = _evaluate(_ledger(control), as_of=date(2026, 12, 2))
    assert not verdict["ok"]
    assert verdict["blocked_ids"] == ["SCA-TEST-01"]


def test_repo_file_claim_must_match_the_checkout():
    good = _control(verification={"method": "repo_file", "path": "SECURITY.md"})
    assert _evaluate(_ledger(good))["ok"]
    bad = _control(verification={"method": "repo_file", "path": "does-not-exist.md"})
    verdict = _evaluate(_ledger(bad))
    assert not verdict["ok"]
    assert verdict["blocked_ids"] == ["SCA-TEST-01"]


def test_repo_file_claim_may_require_a_marker():
    marker = _control(verification={"method": "repo_file", "path": "SECURITY.md",
                                    "contains": "Trust Model"})
    assert _evaluate(_ledger(marker))["ok"]
    missing = _control(verification={"method": "repo_file", "path": "SECURITY.md",
                                     "contains": "no such marker exists"})
    assert not _evaluate(_ledger(missing))["ok"]


def test_unknown_verification_method_blocks():
    verdict = _evaluate(_ledger(_control(verification={"method": "vibes"})))
    assert not verdict["ok"]


# ── accepted risk: the original result is retained, the exception is beside it ─

def _exception(expires="2026-12-31"):
    return {
        "authority": "hermes-core maintainers",
        "rationale": "scan runs nightly out of band; tracked by SEC-EX-1",
        "compensating_control": "nightly osv-scanner schedule",
        "expires": expires,
        "original_result": "unknown",
    }


def test_accepted_risk_keeps_the_original_result_and_passes_only_with_a_valid_exception():
    control = _control(result="accepted_risk", exception=_exception())
    verdict = _evaluate(_ledger(control), as_of=date(2026, 11, 1))
    assert verdict["ok"]
    assert verdict["rows"][0]["original_result"] == "unknown"
    assert verdict["rows"][0]["result"] == "accepted_risk"


def test_accepted_risk_without_exception_blocks():
    verdict = _evaluate(_ledger(_control(result="accepted_risk", exception=None)))
    assert not verdict["ok"]


def test_expired_exception_blocks_again():
    control = _control(result="accepted_risk", exception=_exception(expires="2026-10-10"))
    verdict = _evaluate(_ledger(control), as_of=date(2026, 11, 1))
    assert not verdict["ok"]


def test_exception_must_name_authority_compensating_control_and_expiry():
    for field in ("authority", "compensating_control", "expires", "original_result"):
        exc = _exception()
        del exc[field]
        verdict = _evaluate(_ledger(_control(result="accepted_risk", exception=exc)))
        assert not verdict["ok"], field


# ── CLI contract: what CI actually runs ───────────────────────────────────────

def _run(*argv):
    return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          check=False)


def test_cli_passes_on_the_committed_ledger(tmp_path):
    proc = _run("--ledger", str(LEDGER), "--as-of", "2026-10-19")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_fails_on_a_ledger_with_a_required_unknown(tmp_path):
    doc = json.loads(LEDGER.read_text(encoding="utf-8"))
    doc["controls"][0]["result"] = "unknown"
    doc["controls"][0]["required"] = True
    bad = tmp_path / "ledger.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    proc = _run("--ledger", str(bad), "--as-of", "2026-10-19")
    assert proc.returncode == 1
    assert doc["controls"][0]["id"] in proc.stdout


def test_cli_json_output_carrows_the_verdict(tmp_path):
    proc = _run("--ledger", str(LEDGER), "--as-of", "2026-10-19", "--format", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert isinstance(payload["rows"], list)


def test_cli_rejects_an_unreadable_ledger(tmp_path):
    proc = _run("--ledger", str(tmp_path / "absent.json"))
    assert proc.returncode == 2


# ── the committed ledger itself ───────────────────────────────────────────────

def test_committed_ledger_parses_and_covers_every_control_class():
    doc = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    controls = doc["controls"]
    assert controls, "ledger must not be empty"
    ids = [c["id"] for c in controls]
    assert len(ids) == len(set(ids))
    results = {c["result"] for c in controls}
    assert results <= {"pass", "fail", "not_applicable", "accepted_risk", "unknown"}
    # Every row is attributable to an exact object, not a branch name.
    for control in controls:
        assert control["scope"], control["id"]
        assert control["observed"], control["id"]
        assert control["owner"], control["id"]


def test_committed_ledger_required_controls_are_all_verified():
    doc = json.loads(LEDGER.read_text(encoding="utf-8"))
    verdict = gate.evaluate(doc, root=REPO, as_of=date(2026, 10, 19))
    assert verdict["ok"], verdict["blocking"]

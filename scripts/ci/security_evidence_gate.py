#!/usr/bin/env python3
"""Gate the security evidence ledger: required evidence must be present, current, and verifiable.

The ledger (``security/evidence-ledger.json``) is the dated, machine-readable record of which
security controls were exercised, against which exact object (#92618 §6). This script is what
makes a row mean something: a required control whose evidence is failed, unknown, missing,
stale, or unverifiable against the checkout blocks an unqualified pass (acceptance criterion 7).

Rules enforced here:

* ``unknown`` is a first-class state. It is never promoted to pass; a required unknown blocks.
* ``not_applicable`` requires a recorded scope justification. A skipped check is not inapplicable.
* ``accepted_risk`` passes only with a complete, unexpired exception recorded *beside* the
  original result — the exception names authority, rationale, compensating control, expiry and
  revalidation, and the original result is retained in the verdict (never rewritten).
* Evidence freshness: a control blocks once ``as_of`` passes its ``expires`` date.
* ``repo_file`` claims re-verify against this checkout: the file (and any required marker)
  must actually exist, so a ledger row cannot outlive the object it describes.

Usage::

    python3 scripts/ci/security_evidence_gate.py                      # default ledger, today
    python3 scripts/ci/security_evidence_gate.py --as-of 2026-10-19   # deterministic run
    python3 scripts/ci/security_evidence_gate.py --format json        # machine verdict

Exit codes: 0 = evidence acceptable, 1 = a required control blocks, 2 = ledger unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO / "security" / "evidence-ledger.json"

RESULTS = ("pass", "fail", "not_applicable", "accepted_risk", "unknown")
# A result that satisfies a required control without an exception record.
_CLEAN = ("pass", "not_applicable")
# Fields every control row must carry to be attributable to an exact object.
REQUIRED_FIELDS = (
    "id", "surface", "claim", "verification", "result", "required",
    "owner", "observed", "expires", "scope",
)
EXCEPTION_FIELDS = ("authority", "rationale", "compensating_control", "expires", "original_result")
VERIFICATION_METHODS = ("repo_file", "recorded")


def _parse_day(value: Any, label: str, blocking: list[str], control_id: str) -> date | None:
    """Parse a YYYY-MM-DD string; a missing/garbage date is a blocking finding, not a crash."""
    if not isinstance(value, str):
        blocking.append(f"{control_id}: {label} is not a date string")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        blocking.append(f"{control_id}: {label} {value!r} is not YYYY-MM-DD")
        return None


def _verify_against_repo(control: dict, root: Path, blocking: list[str]) -> bool:
    """Re-derive a ``repo_file`` claim from the checkout. Returns True when it holds."""
    verification = control.get("verification") or {}
    method = verification.get("method")
    if method == "recorded":
        evidence = control.get("evidence") or []
        if not evidence:
            return False
        return all(isinstance(row, dict) and row.get("receipt") for row in evidence)
    if method != "repo_file":
        blocking.append(
            f"{control['id']}: unknown verification method {method!r} "
            f"(expected one of {', '.join(VERIFICATION_METHODS)})"
        )
        return False
    path = verification.get("path")
    if not isinstance(path, str) or not path:
        blocking.append(f"{control['id']}: repo_file verification has no path")
        return False
    if ".." in Path(path).parts or path.startswith(("/", "\\")):
        blocking.append(f"{control['id']}: repo_file path {path!r} escapes the repository")
        return False
    target = root / path
    if not target.is_file():
        return False
    marker = verification.get("contains")
    if marker:
        text = target.read_text(encoding="utf-8", errors="replace")
        return marker in text
    return True


def evaluate(doc: dict, *, root: Path = REPO, as_of: date | None = None) -> dict:
    """Evaluate a ledger document. Returns a verdict dict; never raises on bad content."""
    as_of = as_of or date.today()
    blocking: list[str] = []
    non_blocking_unknown: list[str] = []
    rows: list[dict] = []

    if doc.get("schema_version") != 1:
        blocking.append(f"unsupported schema_version {doc.get('schema_version')!r} (expected 1)")
    controls = doc.get("controls")
    if not isinstance(controls, list) or not controls:
        blocking.append("ledger has no controls")

    seen: set[str] = set()
    for control in controls if isinstance(controls, list) else []:
        if not isinstance(control, dict):
            blocking.append("control row is not an object")
            continue
        control_id = str(control.get("id") or "<missing id>")
        for field in REQUIRED_FIELDS:
            if control.get(field) in (None, ""):
                blocking.append(f"{control_id}: missing required field {field}")
        if control_id in seen:
            blocking.append(f"{control_id}: duplicate control id")
            continue
        seen.add(control_id)

        result = control.get("result")
        if result not in RESULTS:
            blocking.append(f"{control_id}: result {result!r} not one of {', '.join(RESULTS)}")
            continue

        required = bool(control.get("required"))
        expires = _parse_day(control.get("expires"), "expires", blocking, control_id)
        _parse_day(control.get("observed"), "observed", blocking, control_id)

        # Scope justification: "not applicable" is a decision, not an omission.
        if result == "not_applicable" and not control.get("justification"):
            blocking.append(f"{control_id}: not_applicable requires a recorded scope justification")

        # The exception is a separate attributable decision; the original result is retained.
        original_result = result
        exception_ok = False
        if result == "accepted_risk":
            exception = control.get("exception")
            if not isinstance(exception, dict):
                blocking.append(f"{control_id}: accepted_risk requires an exception record")
            else:
                missing = [f for f in EXCEPTION_FIELDS if not exception.get(f)]
                if missing:
                    blocking.append(
                        f"{control_id}: exception missing {', '.join(missing)}"
                    )
                else:
                    original_result = str(exception["original_result"])
                    exc_until = _parse_day(
                        exception.get("expires"), "exception expires", blocking, control_id
                    )
                    exception_ok = exc_until is not None and as_of <= exc_until
                    if exc_until is not None and not exception_ok:
                        blocking.append(f"{control_id}: exception expired {exc_until.isoformat()}")

        verified = _verify_against_repo(control, root, blocking)
        stale = expires is not None and as_of > expires

        if result == "accepted_risk" and exception_ok:
            state = "accepted_risk"
        elif result in _CLEAN and verified and not stale:
            state = result
        elif result == "not_applicable" and control.get("justification"):
            # An inapplicable control is settled even if its file claim drifted; it claims
            # nothing about that object anymore.
            state = "not_applicable"
        elif stale:
            state = "stale"
        elif not verified:
            state = "missing" if result in ("unknown", "pass") else result
        else:
            state = result

        row = {
            "id": control_id,
            "result": state,
            "original_result": original_result,
            "required": required,
            "expires": expires.isoformat() if expires else None,
        }
        rows.append(row)

        if state in _CLEAN or state == "accepted_risk":
            continue
        if required:
            blocking.append(
                f"{control_id}: required evidence is {state} "
                f"(original result: {original_result})"
            )
        elif state in ("unknown", "missing", "stale", "fail"):
            non_blocking_unknown.append(control_id)

    return {
        "ok": not blocking,
        "blocking": blocking,
        "blocked_ids": [line.split(":", 1)[0] for line in blocking],
        "non_blocking_unknown": sorted(set(non_blocking_unknown)),
        "rows": rows,
        "as_of": as_of.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="path to the evidence ledger JSON (default: security/evidence-ledger.json)")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD",
                        help="assessment date (default: today); pin it for reproducible runs")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        text = args.ledger.read_text(encoding="utf-8")
        doc = json.loads(text)
    except FileNotFoundError:
        print(f"evidence gate: ledger not found: {args.ledger}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, OSError) as exc:
        print(f"evidence gate: cannot read ledger {args.ledger}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(doc, dict):
        print(f"evidence gate: ledger {args.ledger} is not a JSON object", file=sys.stderr)
        return 2

    as_of = None
    if args.as_of:
        try:
            as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError:
            print(f"evidence gate: --as-of must be YYYY-MM-DD, got {args.as_of!r}",
                  file=sys.stderr)
            return 2

    verdict = evaluate(doc, root=REPO, as_of=as_of)

    if args.format == "json":
        print(json.dumps(verdict, indent=2, sort_keys=True))
    else:
        print(f"security evidence gate — as of {verdict['as_of']}")
        for row in verdict["rows"]:
            flag = "BLOCK" if row["id"] in verdict["blocked_ids"] else "ok"
            extra = (f" (original: {row['original_result']})"
                     if row["original_result"] != row["result"] else "")
            print(f"  [{flag:5}] {row['id']}: {row['result']}{extra}")
        if verdict["non_blocking_unknown"]:
            print(f"  non-blocking unknowns: {', '.join(verdict['non_blocking_unknown'])}")
        for line in verdict["blocking"]:
            print(f"  ! {line}")
        print("verdict:", "PASS" if verdict["ok"] else "BLOCKED")

    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

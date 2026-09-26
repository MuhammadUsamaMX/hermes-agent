"""Tirith pre-exec security scanning wrapper: runs the tirith binary as a subprocess to scan
commands for content-level threats (homograph URLs, pipe-to-interpreter, terminal injection).
The exit code is the verdict source of truth (0 allow, 1 block, 2 warn); JSON stdout only
enriches findings. Operational failures (spawn error, timeout, unknown exit) respect
``fail_open``; programming errors propagate. Auto-install: a missing tirith is downloaded from
GitHub releases to $HERMES_HOME/bin/tirith in a background thread -- SHA-256 always verified,
cosign provenance when cosign is on PATH."""

import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from contextlib import suppress

from hermes_constants import (
    get_default_hermes_root, get_hermes_home, get_hermes_home_override, hermes_home_key,
)

logger = logging.getLogger(__name__)
_REPO = "sheeki03/tirith"
# Cosign provenance pinned to the release workflow, not the whole repo.
_COSIGN_IDENTITY_REGEXP = f"^https://github.com/{_REPO}/\\.github/workflows/release\\.yml@refs/tags/v"
_COSIGN_ISSUER = "https://token.actions.githubusercontent.com"

# --- Config helpers ---
def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    return default if val is None else val.lower() in {"1", "true", "yes"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _load_security_config() -> dict:
    """Security settings from config.yaml, with env var overrides."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("security", {}) or {}
    except Exception:
        cfg = {}
    return {
        "tirith_enabled": _env_bool("TIRITH_ENABLED", cfg.get("tirith_enabled", True)),
        "tirith_path": os.getenv("TIRITH_BIN", cfg.get("tirith_path", "tirith")),
        "tirith_timeout": _env_int("TIRITH_TIMEOUT", cfg.get("tirith_timeout", 5)),
        "tirith_fail_open": _env_bool("TIRITH_FAIL_OPEN", cfg.get("tirith_fail_open", True)),
        # Extra "trusted scripts" dirs for the pipe_to_interpreter false positive (#32737); the
        # Hermes bin dirs are always trusted and this only appends to them.
        "trusted_executable_dirs": cfg.get("trusted_executable_dirs") or []}


# --- Module state ---
# Cached path after first resolution. _INSTALL_FAILED means "tried and failed" (distinct
# from None = "not yet tried") so a failed install is not retried per command.
_resolved_path: str | None | bool = None
_INSTALL_FAILED = False
_install_failure_reason: str = ""  # reason tag when _resolved_path is _INSTALL_FAILED
# Routed profiles (multiplexed gateway) resolve their own binary: ``security.tirith_path`` and
# ``<home>/bin/tirith`` are per profile, so the launch profile's slot above must not answer for them.
_resolved_path_by_home: dict[str, str] = {}

# Circuit breaker: after _CRASH_LIMIT consecutive spawn/execution failures tirith is disabled so a broken
# binary can't turn every tool call into a fail-open retry loop (#41400). The breaker HALF-OPENS after
# _CIRCUIT_RETRY_S: one caller re-probes tirith for real, and any completed scan (exit 0/1/2 — allow/block/warn
# all prove the binary is healthy) closes it, while a failed probe re-arms the timer. Without the TTL this was
# a one-way latch: once open, the reset branch below was unreachable for the rest of the process.
# Thread safety: crash counting stays lock-free — a racing double-increment only opens the breaker one call
# early, which is harmless, and matches the mcp_tool.py error counters rather than the locked _warn_once
# pattern. _breaker_lock guards ONLY the half-open claim (TTL check + timestamp re-arm, nanoseconds); it is
# never held across the subprocess probe, so it cannot reintroduce the #41400 hang. Claiming re-arms
# _circuit_open_at first, so concurrent callers see a fresh TTL and stay fail-open: one probe per TTL window.
_CRASH_LIMIT = 3
_CIRCUIT_RETRY_S = 300  # half-open probe interval (seconds)
_crash_count: int = 0
_circuit_open: bool = False
_circuit_open_at: float = 0.0
_breaker_lock = threading.Lock()

_install_lock = threading.Lock()
_install_thread: threading.Thread | None = None

# Warn-once: spawn/path warnings sit in the hot path and would otherwise repeat once per
# terminal command while tirith is unavailable (e.g. install thread still running).
_warned_messages: set[str] = set()
_warned_lock = threading.Lock()

_MARKER_TTL = 86400  # disk failure marker validity (24h) -- avoids retry across restarts


def _record_tirith_crash() -> None:
    global _crash_count, _circuit_open, _circuit_open_at
    _crash_count += 1
    if _crash_count >= _CRASH_LIMIT:
        _circuit_open, _circuit_open_at = True, time.monotonic()
        logger.warning("tirith circuit breaker opened after %d consecutive failures; "
                       "disabling for %ds", _crash_count, _CIRCUIT_RETRY_S)


def _warn_once(key: str, message: str, *args) -> None:
    """``logger.warning`` at most once per ``key`` for the process lifetime."""
    with _warned_lock:
        if key in _warned_messages:
            return
        _warned_messages.add(key)
    logger.warning(message, *args)


def _cached_path() -> str | None:
    """The path resolved on a previous call, or None if unresolved (None) / failed (_INSTALL_FAILED)."""
    if get_hermes_home_override() is not None:
        return _resolved_path_by_home.get(hermes_home_key())
    return _resolved_path or None


def _store_resolved(path: str) -> None:
    global _resolved_path
    if get_hermes_home_override() is not None:
        _resolved_path_by_home[hermes_home_key()] = path
    else:
        _resolved_path = path


def _set_resolved(path: str) -> None:
    global _install_failure_reason
    _store_resolved(path)
    _install_failure_reason = ""


def _set_failed(reason: str) -> None:
    global _resolved_path, _install_failure_reason
    _resolved_path, _install_failure_reason = _INSTALL_FAILED, reason


# --- Disk failure marker ---
def _failure_marker_path() -> str:
    return os.path.join(str(get_hermes_home()), ".tirith-install-failed")


def _read_failure_reason() -> str | None:
    """The marker's reason, or None if absent or older than _MARKER_TTL."""
    try:
        p = _failure_marker_path()
        if (time.time() - os.path.getmtime(p)) >= _MARKER_TTL:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _is_install_failed_on_disk() -> bool:
    """True if a recent install failure was persisted and is still non-retryable.
    A 'cosign_missing' marker is auto-cleared once cosign appears on PATH."""
    reason = _read_failure_reason()
    if reason == "cosign_missing" and shutil.which("cosign"):
        _clear_install_failed()
        return False
    return reason is not None


def _mark_install_failed(reason: str = ""):
    """Persist install failure to disk; ``reason`` is a short retryability tag."""
    with suppress(OSError):
        p = _failure_marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(reason)


def _clear_install_failed():
    """Remove the failure marker and reset warn-once state (so a failure after a reinstall surfaces again)."""
    with _warned_lock:
        _warned_messages.clear()
    with suppress(OSError):
        os.unlink(_failure_marker_path())


def _disk_marker_blocks_install() -> bool:
    """Apply a still-valid disk marker to module state; True if install must be skipped.
    Keeps the marker's real reason so in-process retry can detect cosign_missing."""
    if (disk_reason := _read_failure_reason()) is None or not _is_install_failed_on_disk():
        return False
    _set_failed(disk_reason)
    return True


# --- Auto-install ---
def _hermes_bin_dir() -> str:
    """$HERMES_HOME/bin, created if needed."""
    os.makedirs(d := os.path.join(str(get_hermes_home()), "bin"), exist_ok=True)
    return d


# Rust target triple components. Android (Termux) is ABI-compatible with Linux. Windows is
# absent on purpose (no tirith build): None = "never available here", pattern guards still run.
_TARGET_PLATFORMS = {"Darwin": "apple-darwin", "Linux": "unknown-linux-gnu", "Android": "unknown-linux-gnu"}
_TARGET_ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def _detect_target() -> str | None:
    """Rust target triple for this platform, or None if tirith has no build for it."""
    plat = _TARGET_PLATFORMS.get(platform.system())
    arch = _TARGET_ARCHES.get(platform.machine().lower())
    return f"{arch}-{plat}" if plat and arch else None


def is_platform_supported() -> bool:
    """True when tirith ships a prebuilt binary for this OS+arch (CLI banner uses this)."""
    return _detect_target() is not None


def _download_file(url: str, dest: str, timeout: int = 10):
    from agent.secret_scope import get_secret
    req = urllib.request.Request(url)
    if token := get_secret("GITHUB_TOKEN"):
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _verify_cosign(checksums_path: str, sig_path: str, cert_path: str) -> bool | None:
    """Cosign provenance of checksums.txt: True verified, False rejected, None if cosign absent/failed."""
    if not (cosign := shutil.which("cosign")):
        logger.info("cosign not found on PATH")
        return None
    try:
        result = subprocess.run(
            [cosign, "verify-blob", "--certificate", cert_path, "--signature", sig_path,
             "--certificate-identity-regexp", _COSIGN_IDENTITY_REGEXP,
             "--certificate-oidc-issuer", _COSIGN_ISSUER, checksums_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("cosign execution failed: %s", exc)
        return None
    if result.returncode:
        logger.warning("cosign verification failed (exit %d): %s", result.returncode, result.stderr.strip())
        return False
    logger.info("cosign provenance verification passed")
    return True


def _verify_release_provenance(base_url: str, tmpdir: str, checksums_path: str, log) -> tuple[bool, str]:
    """Cosign step of the install -> ``(cosign_verified, failure_reason)``. Only an explicit
    cosign rejection aborts; missing/broken cosign or artifacts fall back to SHA-256 only."""
    if not shutil.which("cosign"):
        logger.info("cosign not on PATH — installing tirith with SHA-256 verification only "
                    "(install cosign for full supply chain verification)")
        return False, ""
    sig_path, cert_path = os.path.join(tmpdir, "checksums.txt.sig"), os.path.join(tmpdir, "checksums.txt.pem")
    try:
        _download_file(f"{base_url}/checksums.txt.sig", sig_path)
        _download_file(f"{base_url}/checksums.txt.pem", cert_path)
    except Exception as exc:
        logger.info("cosign artifacts unavailable (%s), proceeding with SHA-256 only", exc)
        return False, ""
    verified = _verify_cosign(checksums_path, sig_path, cert_path)
    if verified is False:
        log("tirith install aborted: cosign provenance verification failed")
        return False, "cosign_verification_failed"
    if verified is None:
        logger.info("cosign execution failed, proceeding with SHA-256 only")
    return verified is True, ""


def _verify_checksum(archive_path: str, checksums_path: str, archive_name: str) -> bool:
    """Verify SHA-256 of the archive against checksums.txt ("<hash>  <filename>" lines)."""
    with open(checksums_path, encoding="utf-8") as f:
        parsed = (line.strip().split("  ", 1) for line in f)
        expected = next((h for h, *n in parsed if n == [archive_name]), None)
    if not expected:
        logger.warning("No checksum entry for %s", archive_name)
        return False
    sha = hashlib.sha256()
    with open(archive_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    actual = sha.hexdigest()
    if actual != expected:
        logger.warning("Checksum mismatch: expected %s, got %s", expected, actual)
    return actual == expected


def _extract_tirith_binary(tar: tarfile.TarFile, dest_dir: str, log) -> tuple[str | None, str]:
    """Extract the tirith binary from a release archive into dest_dir -> ``(path, reason)``."""
    for member in tar.getmembers():
        if member.name.rsplit("/", 1)[-1] != "tirith" or ".." in member.name:
            continue
        if not member.isfile():
            log("tirith archive member is not a regular file: %s", member.name)
            return None, "binary_not_regular_file"
        if (src_file := tar.extractfile(member)) is None:
            log("tirith binary could not be read from archive")
            return None, "binary_extract_failed"
        dest_path = os.path.join(dest_dir, "tirith")
        with src_file, open(dest_path, "wb") as out:
            shutil.copyfileobj(src_file, out)
        return dest_path, ""
    log("tirith binary not found in archive")
    return None, "binary_not_in_archive"


def _install_tirith(*, log_failures: bool = True) -> tuple[str | None, str]:
    """Download and install tirith to $HERMES_HOME/bin/tirith -> ``(installed_path,
    failure_reason)``; the reason ("" on success) is the disk marker's retryability tag."""
    log = logger.warning if log_failures else logger.debug
    if not (target := _detect_target()):
        logger.info("tirith auto-install: unsupported platform %s/%s", platform.system(), platform.machine())
        return None, "unsupported_platform"
    archive_name = f"tirith-{target}.tar.gz"
    base_url = f"https://github.com/{_REPO}/releases/latest/download"
    try:
        tmpdir = tempfile.mkdtemp(prefix="tirith-install-")
    except OSError as exc:
        log("tirith install failed: cannot create temp dir: %s", exc)
        return None, "no_space"
    try:
        archive_path, checksums_path = os.path.join(tmpdir, archive_name), os.path.join(tmpdir, "checksums.txt")
        logger.info("tirith not found — downloading latest release for %s...", target)
        try:
            _download_file(f"{base_url}/{archive_name}", archive_path)
            _download_file(f"{base_url}/checksums.txt", checksums_path)
        except Exception as exc:
            log("tirith download failed: %s", exc)
            return None, "download_failed"
        cosign_verified, reason = _verify_release_provenance(base_url, tmpdir, checksums_path, log)
        if reason:
            return None, reason
        if not _verify_checksum(archive_path, checksums_path, archive_name):
            return None, "checksum_failed"
        with tarfile.open(archive_path, "r:gz") as tar:
            src, reason = _extract_tirith_binary(tar, tmpdir, log)
        if src is None:
            return None, reason
        dest = os.path.join(_hermes_bin_dir(), "tirith")
        try:
            shutil.move(src, dest)
        except OSError:
            # Cross-device move (Docker, NFS): copy2's metadata step can raise PermissionError,
            # so fall back to plain copy + chmod; a partial dest is removed to avoid a
            # non-executable retry loop.
            try:
                shutil.copy(src, dest)
            except OSError:
                with suppress(OSError):
                    os.unlink(dest)
                return None, "cross_device_copy_failed"
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        logger.info("tirith installed to %s (%s)", dest, "cosign + SHA-256" if cosign_verified else "SHA-256 only")
        return dest, ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- Path resolution ---
def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _find_local_tirith() -> str | None:
    """Cheap local lookup for the default "tirith": PATH, then $HERMES_HOME/bin."""
    hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
    return shutil.which("tirith") or (hermes_bin if _is_executable(hermes_bin) else None)


def _resolve_locally(configured_path: str, *, warn_missing: bool) -> tuple[str | None, bool]:
    """Network-free resolution -> ``(path, may_install)``: ``path`` set = resolved (module state
    updated); else ``may_install`` False = terminal miss (explicit path missing, cached non-retryable
    failure), True = the disk marker / install step may proceed."""
    global _resolved_path, _install_failure_reason
    expanded = os.path.expanduser(configured_path)
    # An explicit (non-"tirith") path is authoritative: never auto-download a replacement.
    if configured_path != "tirith":
        if found := (expanded if _is_executable(expanded) else shutil.which(expanded)):
            _store_resolved(found)
            return found, False
        if warn_missing:
            logger.warning("Configured tirith path %r not found; scanning disabled", configured_path)
        _set_failed("explicit_path_missing")
        return None, False
    # Always re-run the cheap local checks so a manual install is picked up even after a
    # previous network failure (a long-lived gateway recovers without restart).
    if found := _find_local_tirith():
        _set_resolved(found)
        _clear_install_failed()
        return found, False
    # Previous install failed: skip the network retry unless the retryable cosign_missing
    # cause has been resolved in-process.
    if _resolved_path is _INSTALL_FAILED:
        if _install_failure_reason != "cosign_missing" or not shutil.which("cosign"):
            return None, False
        _resolved_path, _install_failure_reason = None, ""
        _clear_install_failed()
    return None, True


def _record_install_result(installed: str | None, reason: str) -> str | None:
    """Cache an install outcome in module state + disk marker; returns *installed*."""
    if installed:
        _set_resolved(installed)
        _clear_install_failed()
    else:
        _set_failed(reason)
        _mark_install_failed(reason)
    return installed


def _resolve_tirith_path(configured_path: str) -> str:
    """Resolve the tirith path, auto-installing synchronously if needed (default "tirith": PATH →
    $HERMES_HOME/bin/tirith → install; failures cached in-process and on disk for 24h). On a miss
    the expanded configured path is returned so the spawn fails open via the dedupe'd OSError."""
    if cached := _cached_path():
        return cached
    expanded = os.path.expanduser(configured_path)
    # No tirith build for this platform: cache the verdict; the spawn fails open once, then
    # the fast path above short-circuits.
    if configured_path == "tirith" and not is_platform_supported():
        _set_failed("unsupported_platform")
        return expanded
    found, may_install = _resolve_locally(configured_path, warn_missing=True)
    if found or not may_install:
        return found or expanded
    # A background install is running: don't start a parallel one; fail-open until it finishes.
    if _install_running() or _disk_marker_blocks_install():
        return expanded
    installed = _record_install_result(*_install_tirith())
    return installed or expanded


def _install_running() -> bool:
    return _install_thread is not None and _install_thread.is_alive()


def _background_install(*, log_failures: bool = True):
    """Background thread target: download and install tirith."""
    with _install_lock:
        if _resolved_path is not None:  # another thread resolved meanwhile
            return
        if found := _find_local_tirith():  # may have been installed by another process
            _set_resolved(found)
            return
        _record_install_result(*_install_tirith(log_failures=log_failures))


def ensure_installed(*, log_failures: bool = True):
    """Resolved path if available now, else None after kicking off a daemon-thread download (local
    checks are synchronous; the download never blocks startup). Safe to call repeatedly."""
    global _install_thread
    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return None
    if cached := _cached_path():
        return cached if _is_executable(cached) else None
    # No tirith build here (e.g. Windows): stay silent -- no PATH probe, no download thread,
    # no disk marker. Pattern-matching guards still run.
    if not is_platform_supported():
        _set_failed("unsupported_platform")
        return None
    found, may_install = _resolve_locally(cfg["tirith_path"], warn_missing=False)
    if found or not may_install or _disk_marker_blocks_install():
        return found
    if not _install_running():
        _install_thread = threading.Thread(target=_background_install, daemon=True,
                                           kwargs={"log_failures": log_failures})
        _install_thread.start()
    return None  # not available yet; commands fail-open until ready


# --- Main API ---
_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500
_EXIT_ACTIONS = {0: "allow", 1: "block", 2: "warn"}
# Summary when tirith's JSON is unparseable and only the exit code is known.
_NO_DETAILS_SUMMARY = {
    "block": "security issue detected (details unavailable)",
    "warn": "security warning detected (details unavailable)"}
_VARIATION_SELECTOR_16 = "\ufe0f"
# Code points that carry the Unicode ``Emoji`` property and take VS16 for emoji presentation: the
# Miscellaneous Symbols / Dingbats blocks, the SMP emoji planes, and the BMP singletons outside them
# (©️ ®️ ‼️ ⁉️ ™️ ℹ️ arrows, ⌚ ⌨️ ⏏️ media keys, Ⓜ️ ▪️ ▶️ ◀️ ◻️ ⤴️ ⬅️ ⬛ ⭐ ⭕ 〰️ 〽️ ㊗️ ㊙️).
# Digits, ``#`` and ``*`` also carry the property (keycap bases) but are deliberately absent: VS16
# after a letter or digit is exactly the steganography signal the rule exists for.
_EMOJI_PRESENTATION_BASE_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122),
    (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328),
    (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1FAFF))


def _verdict(action: str, summary: str = "", findings: list | None = None) -> dict:
    return {"action": action, "findings": [] if findings is None else findings, "summary": summary}


def _fail(fail_open: bool, open_summary: str, closed_summary: str) -> dict:
    return _verdict("allow", open_summary) if fail_open else _verdict("block", closed_summary)


def _crash(fail_open: bool, open_summary: str, closed_summary: str) -> dict:
    """An operational failure: count it toward the circuit breaker, then fail open/closed."""
    _record_tirith_crash()
    return _fail(fail_open, open_summary, closed_summary)


def check_command_security(command: str) -> dict:
    """Run the tirith scan on a command -> ``{"action": allow|warn|block, "findings", "summary"}``.
    Exit code determines the action; JSON enriches. Spawn failures/timeouts respect fail_open."""
    global _crash_count, _circuit_open, _circuit_open_at
    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return _verdict("allow")
    # Circuit breaker: if tirith has crashed _CRASH_LIMIT times in a row, stop trying and fail open (issue
    # #41400). After _CIRCUIT_RETRY_S the breaker half-opens: exactly one caller claims the probe slot —
    # claiming re-arms _circuit_open_at under _breaker_lock, so concurrent callers see a fresh TTL and stay
    # fail-open — and falls through to a real scan below.
    if _circuit_open:
        with _breaker_lock:
            if _circuit_open and time.monotonic() - _circuit_open_at < _CIRCUIT_RETRY_S:
                return _verdict("allow", "tirith disabled (circuit breaker)")
            if _circuit_open:  # TTL expired: claim the single-flight probe slot for this window
                _circuit_open_at = time.monotonic()
                logger.info("tirith circuit breaker half-open: probing after %ds", _CIRCUIT_RETRY_S)
    # No binary for this platform, ever: skip the resolver so we never spawn.
    if not is_platform_supported():
        return _verdict("allow")
    tirith_path = _resolve_tirith_path(cfg["tirith_path"])
    timeout, fail_open = cfg["tirith_timeout"], cfg["tirith_fail_open"]
    if tirith_path is None:
        _warn_once("tirith_path_none", "tirith path resolved to None; scanning disabled")
        return _fail(fail_open, "tirith path unavailable", "tirith path unavailable (fail-closed)")
    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive", "--shell", "posix", "--", command],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            stdin=subprocess.DEVNULL)
    except OSError as exc:
        # FileNotFoundError / PermissionError / exec format error: dedupe by (class, errno)
        # so each failure mode surfaces once, not per command.
        _warn_once(f"tirith_spawn_failed:{type(exc).__name__}:{getattr(exc, 'errno', '')}",
                   "tirith spawn failed: %s", exc)
        return _crash(fail_open, f"tirith unavailable: {exc}", f"tirith spawn failed (fail-closed): {exc}")
    except subprocess.TimeoutExpired:
        _warn_once(f"tirith_timeout:{timeout}", "tirith timed out after %ds", timeout)
        return _crash(fail_open, f"tirith timed out ({timeout}s)", "tirith timed out (fail-closed)")
    exit_code = result.returncode
    if (action := _EXIT_ACTIONS.get(exit_code)) is None:
        # Unknown exit code (includes signal-killed, e.g. -11): respect fail_open.
        logger.warning("tirith returned unexpected exit code %d", exit_code)
        return _crash(fail_open, f"tirith exit code {exit_code} (fail-open)",
                      f"tirith exit code {exit_code} (fail-closed)")
    # Any completed scan (allow/block/warn) proves the binary is healthy: clear the streak and close the
    # breaker. This is the half-open probe's recovery path, and it also fixes the streak never resetting on
    # block/warn verdicts.
    _crash_count = 0
    if _circuit_open:
        _circuit_open, _circuit_open_at = False, 0.0
        logger.info("tirith circuit breaker closed after successful probe")
    # JSON enriches findings/summary; a parse failure never changes the verdict.
    findings, summary = [], ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        findings = data.get("findings", [])[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        logger.debug("tirith JSON parse failed, using exit code only")
        summary = _NO_DETAILS_SUMMARY.get(action, "")
    # .app is a legitimate gTLD: a warn consisting solely of lookalike_tld findings for .app is a
    # known false positive and is downgraded to allow. Any other finding keeps the warn.
    if action == "warn" and findings and all(_is_app_tld_finding(f) for f in findings):
        return _verdict("allow")
    # VS16 follows ordinary emoji-capable code points in standard emoji-presentation sequences.
    # Preserve warnings for every other selector, including VS16 after text, because those can
    # carry the steganographic payload that Tirith is intended to detect.
    if action == "warn" and findings and all(_is_emoji_variation_selector_finding(f) for f in findings) \
            and _has_only_emoji_presentation_selectors(command):
        return _verdict("allow")
    # Tirith matches the SHAPE ``<stage> | <interpreter>``, so it reports the user's own wrapper
    # piped through python3 as HIGH exactly like ``curl | sh`` (#32737). Remote pipes carry their
    # own findings (curl_pipe_shell / wget_pipe_shell / plain_http_to_sink / …), so when every
    # finding is pipe_to_interpreter and every producer feeding the flagged interpreter is a
    # user-owned executable from a trusted scripts directory, the report is a false positive and
    # is downgraded to allow. Any producer that cannot be resolved fails closed.
    if action in ("block", "warn") and findings and all(
            _is_pipe_to_interpreter_finding(f) for f in findings) \
            and _local_pipe_false_positive(command, cfg):
        logger.debug("tirith pipe_to_interpreter downgraded: every producer is a local "
                     "user-owned executable in a trusted directory (#32737)")
        return _verdict("allow")
    return _verdict(action, summary, findings)


def _is_app_tld_finding(finding: dict) -> bool:
    """True if this finding is a lookalike_tld warning for the .app TLD only."""
    if not isinstance(finding, dict) or finding.get("rule_id") != "lookalike_tld":
        return False
    return any(
        val is not None and ".app" in str(val).lower()
        for val in (finding.get(k) for k in ("value", "tld", "detail", "description", "message")))


def _is_emoji_variation_selector_finding(finding: dict) -> bool:
    """True only for the Tirith rule that reports variation selectors."""
    return isinstance(finding, dict) and finding.get("rule_id") == "variation_selector"


def _has_only_emoji_presentation_selectors(command: str) -> bool:
    """Whether every variation selector is VS16 immediately after an emoji-capable base."""
    selectors = ("\ufe00", "\U000e0100")
    saw_selector = False
    for idx, char in enumerate(command):
        if not selectors[0] <= char <= "\ufe0f" and not selectors[1] <= char <= "\U000e01ef":
            continue
        saw_selector = True
        if char != _VARIATION_SELECTOR_16 or idx == 0:
            return False
        base = ord(command[idx - 1])
        if not any(start <= base <= end for start, end in _EMOJI_PRESENTATION_BASE_RANGES):
            return False
    return saw_selector


# --- Pipe-to-interpreter false positives on local producers (#32737) -------------------------------------------------
# Tirith's ``pipe_to_interpreter`` rule only sees the shape ``<stage> | <interpreter>``; it cannot
# tell a remote download piped into a shell from the user's own wrapper filtered through python3,
# so a kanban worker running ``my-wrapper | python3`` was blocked exactly like ``curl | sh``.
# Remote pipes are covered by their own rules (``curl_pipe_shell``, ``wget_pipe_shell``,
# ``plain_http_to_sink``), so this module only has to recognise the local case: every producer
# feeding the flagged interpreter must resolve to a user-owned, non-world-writable executable
# inside a trusted scripts directory. Anything that cannot be resolved (substitution, subshell,
# a name nobody can find, a symlink out of the directory) fails closed and keeps the block.

_PIPE_RULE_ID = "pipe_to_interpreter"

# Interpreters that read their stdin as code. Deliberately narrow: an unrecognised consumer name
# is not counted as a pipe-into-interpreter, so it becomes a producer that must itself be trusted.
_INTERPRETER_RE = re.compile(
    r"(?:sh|bash|zsh|ksh|dash|csh|tcsh|fish|ash|busybox|python[0-9.]*|pypy[0-9.]*|"
    r"node(?:js)?|perl[0-9.]*|ruby[0-9.]*|php[0-9.]*|lua[0-9.]*|bun|deno|pwsh|powershell)",
    re.IGNORECASE)

# Producers that can pull remote content into the pipe even when they sit in a directory the user
# listed as trusted: a careless ``trusted_executable_dirs: [/usr/bin]`` must not turn
# ``nc … | sh`` into an allowed remote pipe.
_FETCHER_NAMES = frozenset({
    "curl", "wget", "http", "https", "httpie", "xh", "aria2", "aria2c",
    "nc", "ncat", "netcat", "socat", "telnet", "ftp", "tftp", "scp", "sftp",
    "rsync", "ssh", "git", "openssl", "cloudflared", "tar"})


def _is_pipe_to_interpreter_finding(finding) -> bool:
    """True only for the Tirith rule that reports piping into an interpreter."""
    return isinstance(finding, dict) and finding.get("rule_id") == _PIPE_RULE_ID


def _split_pipeline_groups(command: str) -> list[list[str]] | None:
    """Split *command* into statements, each a list of its pipeline stages, or None when it uses
    syntax this analysis cannot reason about — command substitution, subshells, brace groups — in
    which case the caller fails closed. Quoting, escapes, comments and ``2>&1`` style redirects
    are honoured; ``||``/``&&``/``;``/``&``/newline end a statement, a bare ``|`` ends a stage.
    """
    from tools.approval_detection import _scan_shell

    statements: list[list[str]] = []
    stages: list[str] = []
    start = 0
    skip_to = -1

    def close_statement(end: int) -> None:
        nonlocal stages
        if end > start:
            stages.append(command[start:end].strip())
        if any(stages):
            statements.append(stages)
        stages = []

    for kind, i, j, quote in _scan_shell(command, subst="u", comments=True):
        if kind == "subst":  # $(…) / backticks: the content's origin is unknowable
            return None
        if i < skip_to:  # second half of a ``||`` already consumed with the statement break
            continue
        if kind == "comment":
            if quote is None:
                close_statement(i)
                start = j
            continue
        if kind != "char" or quote is not None:
            continue
        ch = command[i]
        if ch in "(){}":  # subshell, process substitution, brace group: fail closed
            return None
        if ch == "|":
            if i + 1 < len(command) and command[i + 1] == "|":
                close_statement(i)
                start, skip_to = i + 2, i + 2
            else:
                stages.append(command[start:i].strip())
                start = j
        elif ch == ";" or ch == "\n" or (ch == "&" and not (i and command[i - 1] in "<>")):
            close_statement(i)
            start = j
    close_statement(len(command))
    return statements or None


def _stage_command_word(stage: str) -> str | None:
    """The literal command word of *stage*, or None when it is not a simple literal command
    (bad quoting, a ``VAR=value`` prefix, or anything carrying ``$``/backticks/redirect syntax)."""
    try:
        tokens = shlex.split(stage, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    head = tokens[0]
    if not head or any(ch in head for ch in "$`;&|<>(){}") or head[0] in "=#":
        return None
    if "=" in head and not head.startswith((".", "/", "~")):
        return None  # an assignment, not a command
    return head


def _trusted_executable_dirs(cfg: dict) -> list[str]:
    """Directories whose executables count as "scripts the user owns": the active Hermes home's
    ``bin/``, the root home's ``bin/`` and ``profiles/*/bin/`` (a kanban worker's wrapper lives in
    its own profile), the standard user script dirs, plus ``security.trusted_executable_dirs``."""
    home, root = get_hermes_home(), get_default_hermes_root()
    dirs = [str(home / "bin"), str(root / "bin")]
    profiles = root / "profiles"
    try:
        dirs.extend(str(profiles / name / "bin") for name in os.listdir(profiles))
    except OSError:
        pass
    # expanduser("~") follows $HOME, which Hermes rewrites to the profile home, so the login
    # directory is read from passwd too — a wrapper in the real ~/.local/bin is still the user's.
    home_dirs = {os.path.expanduser("~")}
    with suppress(Exception):
        import pwd
        login_home = pwd.getpwuid(os.getuid()).pw_dir
        if login_home:
            home_dirs.add(login_home)
    for home_dir in home_dirs:
        if home_dir and home_dir != "~":
            dirs.extend((os.path.join(home_dir, ".local", "bin"),
                         os.path.join(home_dir, "bin")))
    dirs.extend(str(d) for d in (cfg.get("trusted_executable_dirs") or [])
                if isinstance(d, str) and d.strip())
    return dirs


def _is_trusted_executable(path: str, trusted: list[str]) -> bool:
    """True when *path* is a user-owned, executable, non-world-writable regular file that stays
    inside one of *trusted* after symlink resolution."""
    try:
        real = os.path.realpath(path)
        info = os.stat(real)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
        return False
    if info.st_mode & stat.S_IWOTH:  # any local account could rewrite it after the scan
        return False
    owner, uid = getattr(info, "st_uid", None), getattr(os, "getuid", lambda: None)()
    if owner is not None and uid is not None and owner != uid:
        return False
    return any(real == root or real.startswith(root + os.sep)
               for root in (os.path.realpath(d) for d in trusted))


def _resolve_trusted_executable(head: str, trusted: list[str]) -> str | None:
    """Absolute path of *head* when it resolves inside *trusted*, else None. PATH wins for a bare
    name (that is what the shell will run); the trusted dirs are only consulted when PATH misses,
    so a wrapper that exists solely in a profile's ``bin/`` is still recognised."""
    if head.startswith((".", "~")) or os.path.isabs(head):
        found = os.path.expanduser(head)
        if not os.path.isabs(found):
            found = os.path.join(os.getcwd(), found)
    else:
        found = shutil.which(head)
        if found is None:
            found = next((p for p in (os.path.join(d, head) for d in trusted)
                          if _is_executable(p)), None)
    if found is None:
        return None
    return found if _is_trusted_executable(found, trusted) else None


def _local_pipe_false_positive(command: str, cfg: dict) -> bool:
    """True when every ``<producer> | <interpreter>`` pipe on *command* is fed only by user-owned
    executables from a trusted scripts directory, so tirith's ``pipe_to_interpreter`` report is a
    false positive (#32737). No qualifying pipe, or one unresolvable producer, returns False."""
    groups = _split_pipeline_groups(command)
    if not groups:
        return False
    trusted = _trusted_executable_dirs(cfg)
    if not trusted:
        return False
    qualifying_pipes = 0
    for stages in groups:
        for index in range(len(stages) - 1):
            consumer = _stage_command_word(stages[index + 1])
            if consumer is None or not _INTERPRETER_RE.fullmatch(os.path.basename(consumer)):
                continue
            qualifying_pipes += 1
            # Everything upstream of the interpreter produced the piped content, so all of it
            # has to be trusted — one /usr/bin stage keeps the block for the whole pipeline.
            for producer_stage in stages[:index + 1]:
                head = _stage_command_word(producer_stage)
                if head is None:
                    return False
                resolved = _resolve_trusted_executable(head, trusted)
                if resolved is None:
                    return False
                if (_FETCHER_NAMES & {os.path.basename(head).lower(),
                                      os.path.basename(resolved).lower()}):
                    return False
    return qualifying_pipes > 0

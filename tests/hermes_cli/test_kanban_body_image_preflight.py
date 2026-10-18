"""A Kanban card body citing a print-resolution image must not poison its own worker (#124833).

``_collect_kanban_task_images`` hands every local image referenced in a task body to
the worker's first turn. Those paths flow into ``build_native_content_parts`` ->
``_file_to_data_url``, which encodes at *native* size by design ("the agent retry loop
shrinks on rejection"). The reactive pass is gated on ``FailoverReason.image_too_large``,
which needs a status code plus recognisable wording — a provider that enforces its cap
by dropping the connection yields neither, so the payload is re-sent unchanged and the
worker exits non-zero.

Nothing about that loop is specific to the card body, though: a body image is a
*reference for the worker*, not user content anyone needs at print resolution. So the
body path caps its own attachments before they go on the wire, which removes the class
regardless of provider wording or status code, and leaves genuine user uploads (--image)
at native quality with no silent quality tax.

Covered here:
  1. an oversized body image is capped before it reaches the first turn,
  2. a small body image keeps its native path (no proactive quality tax),
  3. the file on disk is never mutated or deleted,
  4. an uncappable image falls back to the original path (never blocks worker startup),
  5. non-Kanban runs and remote URLs are untouched.
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _kanban_worker_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_124833")
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)


def _encoded_len(path) -> int:
    """Size of the base64 data URL ``_file_to_data_url`` would put on the wire."""
    from agent.image_routing import _file_to_data_url

    url = _file_to_data_url(Path(path))
    assert url is not None, f"fixture {path} is not a readable image"
    return len(url)


def _oversized_image(tmp_path: Path) -> Path:
    """A real JPEG whose base64 encoding is well over the 4 MB budget."""
    from PIL import Image

    path = tmp_path / "print_resolution.jpg"
    Image.effect_noise((2400, 2400), 100).convert("RGB").save(path, "JPEG", quality=95)
    return path


def _small_image(tmp_path: Path) -> Path:
    from PIL import Image

    path = tmp_path / "thumb.jpg"
    Image.effect_noise((64, 64), 100).convert("RGB").save(path, "JPEG", quality=90)
    return path


def _stub_task_body(monkeypatch, body: str) -> None:
    """Serve ``body`` as the task record the collector reads off the board DB."""
    from hermes_cli import kanban_db, kanban_db_connect

    monkeypatch.setattr(
        kanban_db_connect, "connect_closing", lambda *a, **k: __import__("contextlib").nullcontext(object())
    )
    monkeypatch.setattr(kanban_db, "get_task", lambda conn, task_id: SimpleNamespace(body=body, id=task_id))


def _collect(images):
    from hermes_cli.cli_single_query import _collect_kanban_task_images

    return _collect_kanban_task_images(images)


def test_oversized_body_image_is_capped_before_the_first_turn(monkeypatch, tmp_path):
    """The reported case: a 39 MB body image reaches the provider at 52 MB and is dropped."""
    from agent.conversation_compression import _IMAGE_SHRINK_TARGET_BYTES

    oversized = _oversized_image(tmp_path)
    assert _encoded_len(oversized) > _IMAGE_SHRINK_TARGET_BYTES, "fixture must exceed the budget"
    _stub_task_body(monkeypatch, f"see sample {oversized}")

    images = []
    _collect(images)

    assert len(images) == 1, "the body image must still be attached — capped, not dropped"
    assert _encoded_len(images[0]) <= _IMAGE_SHRINK_TARGET_BYTES


def test_small_body_image_keeps_its_native_path(monkeypatch, tmp_path):
    """Proactive downscale costs nothing only where it is free: the body path must not tax small images."""
    small = _small_image(tmp_path)
    _stub_task_body(monkeypatch, f"see {small}")

    images = []
    _collect(images)

    assert images == [small]


def test_body_image_file_on_disk_is_never_mutated(monkeypatch, tmp_path):
    """The cap applies to the attachment, not to the reporter's file."""
    oversized = _oversized_image(tmp_path)
    before = (oversized.read_bytes(), oversized.stat().st_size)
    _stub_task_body(monkeypatch, f"see {oversized}")

    images = []
    _collect(images)

    assert oversized.exists(), "the original body image must survive"
    assert (oversized.read_bytes(), oversized.stat().st_size) == before


def test_uncappable_image_falls_back_to_the_original_path(monkeypatch, tmp_path):
    """A corrupt or Pillow-less image must not stop the worker booting; the reactive path still owns it."""
    import agent.conversation_compression as compression

    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not a jpeg at all" * 1000)
    monkeypatch.setattr(compression, "_IMAGE_SHRINK_TARGET_BYTES", 1024)  # force the cap to trigger
    _stub_task_body(monkeypatch, f"see {corrupt}")

    images = []
    _collect(images)

    assert images == [corrupt]


def test_non_kanban_run_attaches_nothing(monkeypatch, tmp_path):
    """Without HERMES_KANBAN_TASK this collector is a no-op — no body, no images, no resizing."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    oversized = _oversized_image(tmp_path)

    images = []
    assert _collect(images) == []
    assert images == []


def test_body_image_urls_are_passed_through_verbatim(monkeypatch, tmp_path):
    """A remote URL is the provider's to fetch; only local files get capped."""
    url = "https://example.invalid/print.jpg"
    _stub_task_body(monkeypatch, f"see {url}")

    images = []
    assert _collect(images) == [url]
    assert images == []


def test_image_arg_dedup_still_works_against_capped_body_paths(monkeypatch, tmp_path):
    """A path the user already passed with --image is not re-added from the body."""
    small = _small_image(tmp_path)
    _stub_task_body(monkeypatch, f"see {small} and {small}")

    images = [small]
    _collect(images)

    assert images == [small]


def test_capped_body_image_is_a_real_decodable_image(monkeypatch, tmp_path):
    """The cap must re-encode, not truncate: tools taking the path handle still open it."""
    import io

    from PIL import Image

    oversized = _oversized_image(tmp_path)
    _stub_task_body(monkeypatch, f"see {oversized}")

    images = []
    _collect(images)

    with Image.open(images[0]) as img:
        img.verify()
    assert images[0].read_bytes()[:2] == b"\xff\xd8", "still a JPEG, not a truncated blob"

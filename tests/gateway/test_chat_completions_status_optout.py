"""``platforms.api_server.tool_progress_events: false`` also drops the custom
``hermes.status`` SSE frames from streaming Chat Completions (#124328).

The opt-out exists so a strict OpenAI SDK client never receives a named ``event:`` frame:
the SDK turns one into a chunk with ``choices=None`` and crashes on it with
``TypeError: 'NoneType' object is not subscriptable``. ``hermes.status`` (provider wait,
auto-recovery countdown, fallback switch) kept streaming while the provider was retried or
recovering — exactly when the client is least able to tolerate it.

``approval.request`` stays: it is an interactive control frame, and hiding it would stall the
turn waiting for an approval answer.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

_STATUS = ("__status__", {"kind": "lifecycle", "text": "❌ Rate limited after 1 retries — HTTP 429"})
_APPROVAL = ("__approval__", {"approvalId": "a1", "tool": "terminal"})


def _stream_body(platform_cfg):
    """Run the real ``_write_sse_chat_completion`` over a status + approval + content stream."""
    from aiohttp import web
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter, ThreadSafeAsyncQueue

    # from_dict is the production loader: a top-level platform key lands in ``extra``.
    adapter = APIServerAdapter(PlatformConfig.from_dict(platform_cfg))
    written = []

    async def fake_agent():
        return {"final_response": "done", "completed": True}, {
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    async def run():
        stream_q = ThreadSafeAsyncQueue()
        stream_q.put_nowait(_STATUS)
        stream_q.put_nowait(_APPROVAL)
        stream_q.put_nowait("done")
        stream_q.put_nowait(None)
        agent_task = asyncio.ensure_future(fake_agent())
        resp = AsyncMock(spec=web.StreamResponse)
        resp.write = AsyncMock(side_effect=lambda data: written.append(data))
        resp.prepare = AsyncMock()
        req = MagicMock()
        req.headers = {}
        with patch("gateway.platforms.api_server.web.StreamResponse", return_value=resp):
            await adapter._write_sse_chat_completion(req, "cmpl-1", "m", 1, stream_q, agent_task)

    asyncio.run(run())
    return b"".join(written).decode()


def test_status_frames_still_stream_by_default():
    """Default config is unchanged: hermes.status still reaches the client."""
    body = _stream_body({"enabled": True, "token": "k"})
    assert "event: hermes.status" in body
    assert '"content": "done"' in body


def test_tool_progress_events_false_suppresses_status_frames():
    """The reported bug: the opt-out left hermes.status on the wire."""
    body = _stream_body({"enabled": True, "token": "k", "tool_progress_events": False})
    assert "hermes.status" not in body
    assert '"content": "done"' in body


def test_tool_progress_events_false_keeps_approval_request_frame():
    """approval.request is an interactive control frame; suppressing it would stall the turn."""
    body = _stream_body({"enabled": True, "token": "k", "tool_progress_events": False})
    assert "event: approval.request" in body


def test_tool_progress_events_false_leaves_no_named_frames_besides_approval():
    """A strict OpenAI client must see no named frame other than the approval control frame."""
    body = _stream_body({"enabled": True, "token": "k", "tool_progress_events": False})
    named = [line for line in body.splitlines() if line.startswith("event: ")]
    assert named == ["event: approval.request"]
"""Lean curated agent (Track-A) — code + files + web, over the local engine.

This is the SMALL, self-contained agent the lean chat UI drives when the user
turns Agent mode on. It deliberately does NOT reuse the heavy odysseus
``stream_agent_loop`` (which is welded to DB sessions / owners / RAG / the full
50-tool surface). Instead it runs a tight loop over the in-process Alice engine
with a CURATED tool set —

    bash, python, read_file, write_file, web_search, web_fetch

— all of which execute locally in-process via ``_call_mcp_tool`` with NO
session/owner coupling (see ``src.tool_execution``). The surface is HARD-gated
on ``agent_mode_enabled()``: every request is 403 when Agent mode is off, and a
defensive whitelist drops any tool the model emits outside the curated six.

Why a new module and not ``stream_agent_loop``: the white-screen rewrite traded
the fragile 100-module odysseus frontend for a lean one; re-adopting the heavy
agent (sessions/RAG/endpoints) would re-import that complexity. This loop reuses
only the proven *pieces* — the runtime token stream (``iter_text_segments``), the
fenced-block parser (``parse_tool_blocks``), and the in-process tool executor
(``execute_tool_block``) — behind one clean SSE endpoint the lean client drives.

Contract — ``POST /alice/agent_stream`` with JSON ``{"messages":[{role,content}]}``
emits ``text/event-stream`` of ``data: {json}`` events:

    {"type":"delta","text":...}          raw assistant token (live typing)
    {"type":"assistant_text","text":...} round's cleaned text (fences stripped)
    {"type":"tool_start","tool":...,"input":...}
    {"type":"tool_output","tool":...,"output":...,"exit_code":?,"ok":bool}
    {"type":"agent_step","round":n}
    {"type":"done","reason":?}
    {"type":"error","message":...}

then a terminal ``data: [DONE]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Iterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# Per-round generation budget. Deliberately higher than the plain-chat default
# (ALICE_AI_MAX_TOKENS, 512) so an agent turn that reasons + emits a tool call
# (or writes a longer final answer) is not truncated mid-thought.
_AGENT_MAX_TOKENS = int(os.getenv("ALICE_AI_AGENT_MAX_TOKENS", "2048"))

# The curated consumer tool set (the product decision: "code + files + web").
# NOTHING outside this set is advertised to the model or permitted to execute —
# a defensive whitelist layered on top of the system prompt + the executor's own
# Simple-mode gate. Keep in sync with the system prompt below.
ALLOWED_TOOLS = frozenset(
    {"bash", "python", "read_file", "write_file", "web_search", "web_fetch"}
)

# Generous round cap so a real multi-step task can finish, with a hard backstop
# against a model that loops calling tools forever.
MAX_ROUNDS = 10

# Single local user — the app is single-user and already authenticated by the
# per-launch loopback token middleware. The curated six ignore owner entirely
# (they route through the session/owner-free ``_call_mcp_tool`` path); this is
# only a stable label for logs.
_LOCAL_OWNER = "local-agent"

_SYSTEM_PROMPT = """\
You are Alice, a local AI assistant running privately on the user's own computer. \
You can use tools to get real work done on this machine.

To use a tool, write a fenced code block whose LANGUAGE TAG is the exact tool name. \
The block runs automatically, you see its output, and then you continue. You have \
exactly these six tools — use the EXACT tag shown, copy the shape exactly:

Run a shell command:
```bash
echo hello
```

Run Python code:
```python
print(2 + 2)
```

Write a file — the FIRST line is the path, everything after it is the file's contents:
```write_file
/tmp/notes.txt
hello world
this is the file body
```

Read a file — the content is just the path:
```read_file
/tmp/notes.txt
```

Search the web for a quick fact:
```web_search
current population of Tokyo
```

Fetch and read a specific URL:
```web_fetch
https://example.com
```

Rules:
- The tag must be EXACTLY one of: bash, python, write_file, read_file, web_search, web_fetch. \
A block tagged anything else (```text, ```sh, ```py, ```json) is treated as plain display text and does NOT run.
- So to actually DO something you MUST use the exact tag above — never describe the action in a ```text block and assume it happened.
- Only use a tool when it actually helps; don't use one for things you already know.
- After a tool runs, read its real output before continuing. Do not invent or predict tool output — wait for it.
- Keep taking steps until the task is genuinely done. When done, stop calling tools and write a short final answer — that IS your "done" signal.
- If a tool fails, don't go silent: fix it and retry, or tell the user plainly what is blocking you.
- You have only these six tools. If the user wants something outside them, say so plainly.
"""


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _normalize_messages(payload: dict) -> list[dict]:
    """Validate + coerce the client message list to ``[{role, content}]``."""
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages must be a non-empty array")
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant", "system") and isinstance(content, str):
            out.append({"role": role, "content": content})
    if not out:
        raise ValueError("no valid messages")
    return out


def _display_output(result: dict) -> str:
    """Pick a human-facing one-shot string from a tool result dict (for the UI
    card). The model still receives the full ``format_tool_result`` text."""
    for key in ("output", "content", "results", "error"):
        val = result.get(key)
        if val:
            return str(val)
    if result.get("path"):
        return f"Saved {result['path']}"
    if result.get("success"):
        return "OK"
    return ""


@router.post("/alice/agent_stream")
async def agent_stream(request: Request):
    """Curated server-side agent loop (code + files + web). 403 unless Agent mode is on."""
    # Hard gate: this surface simply does not exist when Agent mode is off.
    try:
        from core.alice_security import agent_mode_enabled

        on = bool(agent_mode_enabled())
    except Exception:  # noqa: BLE001
        on = False
    if not on:
        return JSONResponse(status_code=403, content={"error": "AGENT_MODE_OFF"})

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        user_messages = _normalize_messages(payload)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    def _gen() -> Iterator[bytes]:
        # Runs in Starlette's threadpool (StreamingResponse iterates a sync
        # generator off-loop), so the blocking model inference does not stall
        # the event loop and we can drive the async tool executor with
        # ``asyncio.run`` on this worker thread (no loop is running here).
        try:
            from alice_provider import iter_text_segments
            # Import agent_tools FIRST: src.tool_parsing and src.agent_tools are
            # mutually referential, so importing tool_parsing *cold* (before
            # agent_tools) trips the cycle. In the live app agent_tools is
            # already loaded at startup; this keeps the module self-sufficient
            # on any import order.
            import src.agent_tools  # noqa: F401
            from src.tool_parsing import parse_tool_blocks, strip_tool_blocks
            from src.tool_execution import execute_tool_block, format_tool_result

            messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + user_messages

            for rnd in range(MAX_ROUNDS):
                # 1) Generate one assistant turn, streaming raw tokens live.
                buf: list[str] = []
                for seg in iter_text_segments(messages, _AGENT_MAX_TOKENS):
                    buf.append(seg)
                    yield _sse({"type": "delta", "text": seg})
                text = "".join(buf)

                # 2) Emit the round's cleaned text so the UI can replace the raw
                #    live buffer (which briefly showed the fenced tool calls)
                #    with clean prose.
                clean = strip_tool_blocks(text).strip()
                yield _sse({"type": "assistant_text", "text": clean})

                # 3) Extract tool calls; keep ONLY the curated six.
                blocks = [
                    b for b in parse_tool_blocks(text) if b.tool_type in ALLOWED_TOOLS
                ]
                if not blocks:
                    yield _sse({"type": "done"})
                    yield b"data: [DONE]\n\n"
                    return

                # Record the assistant turn verbatim (with the fenced calls) so
                # the model keeps a faithful trace of what it asked to run.
                messages.append({"role": "assistant", "content": text})

                # 4) Execute each tool to completion, feeding the result back.
                for b in blocks:
                    inp = b.content.split("\n", 1)[0][:200]
                    yield _sse({"type": "tool_start", "tool": b.tool_type, "input": inp})
                    try:
                        desc, result = asyncio.run(
                            execute_tool_block(b, owner=_LOCAL_OWNER, session_id=None)
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("[alice_agent] tool execution error")
                        desc = f"{b.tool_type}: error"
                        result = {"error": f"{type(exc).__name__}: {exc}", "exit_code": 1}

                    ok = (not result.get("error")) and result.get("exit_code", 0) in (0, None)
                    yield _sse(
                        {
                            "type": "tool_output",
                            "tool": b.tool_type,
                            "output": _display_output(result)[:8000],
                            "exit_code": result.get("exit_code"),
                            "ok": bool(ok),
                        }
                    )
                    messages.append(
                        {"role": "user", "content": format_tool_result(desc, result)}
                    )

                yield _sse({"type": "agent_step", "round": rnd + 1})

            yield _sse({"type": "done", "reason": "max_rounds"})
            yield b"data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[alice_agent] stream error")
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

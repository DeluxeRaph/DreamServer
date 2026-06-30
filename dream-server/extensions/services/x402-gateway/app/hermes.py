from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import websockets


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
TOKEN_RE = re.compile(r'__HERMES_SESSION_TOKEN__="([^"]+)"')


class HermesError(RuntimeError):
    pass


def extract_prompt(payload: dict[str, Any]) -> str:
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()

    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = [
                    str(part.get("text", "")).strip()
                    for part in content
                    if isinstance(part, dict) and part.get("type") in {None, "text"}
                ]
                text = "\n".join(part for part in parts if part)
                if text:
                    return text

    raise HermesError("prompt_or_messages_required")


def normalize_session_id(value: Any) -> str | None:
    if value is None:
        return None
    session_id = str(value).strip()
    if not session_id:
        return None
    if not SESSION_ID_RE.match(session_id):
        raise HermesError("invalid_session_id")
    return session_id


def sse_data(payload: dict[str, Any] | str) -> bytes:
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"data: {body}\n\n".encode("utf-8")


def metadata_chunk(session_id: str, model: str, resumed: bool) -> dict[str, Any]:
    return {
        "type": "metadata",
        "session_id": session_id,
        "model": model,
        "resumed": resumed,
    }


def chat_chunk(model: str, content: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-hermes-{uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }


def final_chunk(model: str, session_id: str, usage: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-hermes-{uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "session_id": session_id,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop" if status == "complete" else status,
            }
        ],
        "usage": usage,
    }


async def _hermes_token(client: httpx.AsyncClient, hermes_url: str, timeout: float) -> str:
    response = await client.get(f"{hermes_url.rstrip('/')}/", timeout=timeout)
    response.raise_for_status()
    match = TOKEN_RE.search(response.text)
    if not match:
        raise HermesError("hermes_session_token_not_found")
    return match.group(1)


async def _rpc(ws: Any, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        )
    )
    while True:
        message = json.loads(await ws.recv())
        if message.get("id") != request_id:
            continue
        if "error" in message:
            detail = message.get("error", {}).get("message") or "hermes_rpc_error"
            raise HermesError(str(detail))
        return message.get("result") or {}


async def _open_session(
    ws: Any,
    *,
    requested_session_id: str | None,
    cols: int,
) -> tuple[str, str, bool]:
    if requested_session_id:
        resumed = await _rpc(ws, 1, "session.resume", {"session_id": requested_session_id, "cols": cols})
        live_session_id = str(resumed.get("session_id") or "")
        durable_session_id = str(resumed.get("resumed") or requested_session_id)
        return live_session_id, durable_session_id, True

    created = await _rpc(ws, 1, "session.create", {"cols": cols})
    live_session_id = str(created.get("session_id") or "")
    title = await _rpc(ws, 2, "session.title", {"session_id": live_session_id})
    durable_session_id = str(title.get("session_key") or live_session_id)
    return live_session_id, durable_session_id, False


async def stream_hermes_chat(
    *,
    payload: dict[str, Any],
    client: httpx.AsyncClient,
    hermes_url: str,
    model: str,
    timeout: float,
    max_output_tokens: int,
) -> AsyncIterator[bytes]:
    prompt = extract_prompt(payload)
    requested_session_id = normalize_session_id(payload.get("session_id"))
    cols = int(payload.get("cols") or 100)
    emitted_chars = 0
    max_output_chars = max(1, max_output_tokens) * 4
    token = await _hermes_token(client, hermes_url, timeout)
    ws_url = f"{hermes_url.rstrip('/').replace('http://', 'ws://').replace('https://', 'wss://')}/api/ws?token={token}"

    async with websockets.connect(ws_url, open_timeout=timeout, ping_interval=None) as ws:
        # gateway.ready
        await asyncio.wait_for(ws.recv(), timeout=timeout)
        live_session_id, durable_session_id, resumed = await _open_session(
            ws,
            requested_session_id=requested_session_id,
            cols=cols,
        )
        yield sse_data(metadata_chunk(durable_session_id, model, resumed))

        await _rpc(
            ws,
            3,
            "prompt.submit",
            {"session_id": live_session_id, "text": prompt},
        )

        final_usage: dict[str, Any] = {}
        final_status = "complete"
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(timeout, 60)))
            params = message.get("params") or {}
            if params.get("session_id") != live_session_id:
                continue
            event_type = params.get("type")
            event_payload = params.get("payload") or {}
            if event_type == "message.delta":
                text = event_payload.get("text")
                if isinstance(text, str) and text:
                    remaining_chars = max_output_chars - emitted_chars
                    if remaining_chars <= 0:
                        yield sse_data(final_chunk(model, durable_session_id, final_usage, "length"))
                        yield sse_data("[DONE]")
                        return
                    if len(text) > remaining_chars:
                        text = text[:remaining_chars]
                    emitted_chars += len(text)
                    yield sse_data(chat_chunk(model, text))
                    if emitted_chars >= max_output_chars:
                        yield sse_data(final_chunk(model, durable_session_id, final_usage, "length"))
                        yield sse_data("[DONE]")
                        return
            elif event_type == "message.complete":
                final_usage = event_payload.get("usage") if isinstance(event_payload.get("usage"), dict) else {}
                final_status = str(event_payload.get("status") or "complete")
                title = await _rpc(ws, 4, "session.title", {"session_id": live_session_id})
                durable_session_id = str(title.get("session_key") or durable_session_id)
                yield sse_data(final_chunk(model, durable_session_id, final_usage, final_status))
                yield sse_data("[DONE]")
                return
            elif event_type == "error":
                detail = event_payload.get("message") or "hermes_stream_error"
                raise HermesError(str(detail))

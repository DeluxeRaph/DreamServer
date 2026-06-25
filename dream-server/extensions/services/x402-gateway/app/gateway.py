from __future__ import annotations

import httpx
from fastapi import Request, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .audit import AuditLog
from .config import RouteRule


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


async def proxy_request(
    request: Request,
    rule: RouteRule,
    client: httpx.AsyncClient,
    audit: AuditLog,
) -> Response:
    body = await request.body()
    upstream_url = str(rule.upstream)
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    stream_context = client.stream(
        request.method,
        upstream_url,
        content=body,
        headers=_forward_headers(request),
    )
    upstream = await stream_context.__aenter__()

    audit.write(
        "paid_request_forwarded",
        {
            "method": request.method,
            "path": request.url.path,
            "upstream": str(rule.upstream),
            "status_code": upstream.status_code,
        },
    )

    return StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
        background=BackgroundTask(stream_context.__aexit__, None, None, None),
    )

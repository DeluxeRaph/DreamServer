from __future__ import annotations

import os
from collections.abc import Callable
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.responses import StreamingResponse
from x402.http.middleware.fastapi import PaymentMiddlewareASGI

from .audit import AuditLog
from .config import CapabilityConfig, GatewayConfig, RouteRule, load_config
from .gateway import proxy_request
from .hermes import HermesError, stream_hermes_chat
from .payments import build_resource_server, build_x402_routes
from .policy import RoutePolicy


CONFIG_PATH = os.environ.get("X402_CONFIG_PATH", "/config/config.json")
AUDIT_LOG_PATH = os.environ.get("X402_AUDIT_LOG", "/data/audit.jsonl")
PORT = int(os.environ.get("X402_GATEWAY_PORT_INTERNAL", "4020"))
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://llama-server:8080")
DASHBOARD_API_URL = os.environ.get("DASHBOARD_API_URL", "http://dashboard-api:3002")
DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "")
RUNTIME_HEALTH_TIMEOUT = float(os.environ.get("X402_RUNTIME_HEALTH_TIMEOUT", "5"))
HERMES_URL = os.environ.get("HERMES_URL", "http://dream-hermes:9119")
HERMES_CHAT_TIMEOUT = float(os.environ.get("X402_HERMES_CHAT_TIMEOUT", "120"))
HERMES_SESSIONS_PATH = os.environ.get("HERMES_SESSIONS_PATH", "/opt/data/sessions")


def create_app(config: GatewayConfig | None = None) -> FastAPI:
    loaded = config or load_config(CONFIG_PATH)
    app = FastAPI(title="Dream Server x402 Gateway")
    app.state.config = loaded
    app.state.policy = RoutePolicy(loaded)
    app.state.audit = AuditLog(AUDIT_LOG_PATH)
    app.state.http = httpx.AsyncClient(timeout=60.0, follow_redirects=False)

    @app.on_event("shutdown")
    async def shutdown_http_client() -> None:
        await app.state.http.aclose()

    @app.get("/health")
    async def legacy_health() -> dict[str, object]:
        return _health_payload(loaded)

    @app.get("/v1/health")
    async def health() -> dict[str, object]:
        return _health_payload(loaded)

    @app.get("/v1/health/ready")
    async def ready() -> dict[str, object]:
        return _readiness_payload(loaded)

    @app.get("/v1/health/runtime")
    async def runtime_health(probe: bool = False) -> dict[str, object]:
        return await _runtime_health_payload(loaded, app.state.http, probe=probe)

    @app.get("/v1/hermes/status")
    async def hermes_status() -> dict[str, object]:
        return await _hermes_status_payload(app.state.http)

    @app.get("/v1/provider")
    async def provider() -> dict[str, object]:
        return _provider_payload(loaded)

    @app.get("/v1/vendor")
    async def vendor() -> dict[str, object]:
        return loaded.vendor.model_dump(mode="json")

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        return {
            "provider": {"id": loaded.vendor.id, "name": loaded.vendor.name},
            "models": [model.model_dump(mode="json") for model in loaded.models],
        }

    @app.get("/v1/limits")
    async def limits() -> dict[str, object]:
        return loaded.limits.model_dump(mode="json")

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, object]:
        return {
            "provider": loaded.vendor.model_dump(mode="json"),
            "capabilities": [
                capability.model_dump(mode="json")
                for capability in _capabilities(loaded)
            ],
        }

    @app.post("/v1/quote")
    async def quote(payload: dict[str, object]) -> dict[str, object]:
        return _quote_payload(loaded, payload)

    if loaded.enabled and not loaded.policy.devBypass:
        app.add_middleware(
            PaymentMiddlewareASGI,
            routes=build_x402_routes(loaded),
            server=build_resource_server(loaded),
        )

    for rule in loaded.rules:
        handler = _handler_for_rule(rule)
        for method in rule.methods:
            app.add_api_route(
                rule.path,
                handler,
                methods=[method],
                name=f"x402_{method.lower()}_{rule.path.strip('/').replace('/', '_')}",
            )

    return app


def _health_payload(config: GatewayConfig) -> dict[str, object]:
    return {
        "status": "ok",
        "service": "dream-server-x402-gateway",
        "version": config.vendor.version,
        "protocolVersion": config.vendor.protocolVersion,
        "enabled": config.enabled,
        "rules": len(config.rules),
    }


def _provider_payload(config: GatewayConfig) -> dict[str, object]:
    payload = config.vendor.model_dump(mode="json")
    payload["providerType"] = "dream-server"
    payload["endpoints"] = {
        "capabilities": "/v1/capabilities",
        "health": "/v1/health",
        "hermesStatus": "/v1/hermes/status",
        "runtimeHealth": "/v1/health/runtime",
        "models": "/v1/models",
        "quote": "/v1/quote",
    }
    return payload


def _readiness_payload(config: GatewayConfig) -> dict[str, object]:
    checks = {
        "api": "ok",
        "capability_registry": "ok" if _capabilities(config) else "down",
        "payment_gateway": "ok" if config.enabled else "disabled",
        "payment_rules": "ok" if config.rules else "down",
        "usage_metering": "ok" if config.audit.logPayments else "disabled",
    }
    status = "ok" if all(value in {"ok", "disabled"} for value in checks.values()) else "degraded"
    return {"status": status, "checks": checks}


def _capabilities(config: GatewayConfig) -> list[CapabilityConfig]:
    if config.capabilities:
        return config.capabilities
    return [_capability_from_rule(rule) for rule in config.rules]


def _capability_from_rule(rule: RouteRule) -> CapabilityConfig:
    capability_id = rule.name.lower().replace(" ", "_").replace("-", "_")
    return CapabilityConfig(
        id=capability_id,
        description=rule.metadata.description or rule.name,
        path=rule.path,
        method=rule.methods[0],
        streaming=True,
        pricing=rule.price,
    )


def _quote_payload(config: GatewayConfig, payload: dict[str, object]) -> dict[str, object]:
    capability_id = str(payload.get("capability", ""))
    model_id = str(payload.get("model", ""))
    capability = next(
        (candidate for candidate in _capabilities(config) if candidate.id == capability_id),
        None,
    )
    if not capability:
        raise HTTPException(status_code=404, detail="capability_not_found")
    if model_id and model_id not in capability.models:
        raise HTTPException(status_code=400, detail="model_not_supported_for_capability")
    if not model_id:
        if not capability.models:
            raise HTTPException(status_code=400, detail="capability_has_no_models")
        model_id = capability.models[0]

    return {
        "quoteId": f"quote_{uuid4().hex}",
        "providerId": config.vendor.id,
        "capability": capability.id,
        "model": model_id,
        "price": capability.pricing.model_dump(mode="json"),
        "payment": {
            "protocol": "x402",
            "method": capability.method,
            "resource": capability.path,
        },
        "streaming": bool(payload.get("stream", capability.streaming)),
    }


async def _runtime_health_payload(
    config: GatewayConfig,
    client: httpx.AsyncClient,
    *,
    probe: bool = False,
) -> dict[str, object]:
    checks: dict[str, str] = {"api": "ok"}
    details: dict[str, object] = {}

    llama_base = LLAMA_SERVER_URL.rstrip("/")
    dashboard_base = DASHBOARD_API_URL.rstrip("/")
    advertised_models = [model.id for model in config.models]
    details["advertisedModels"] = advertised_models

    hermes_status = await _hermes_status_payload(client)
    checks["hermes"] = "ok" if hermes_status["status"] == "ok" else "down"
    details["hermes"] = hermes_status

    try:
        llama_health = await client.get(
            f"{llama_base}/health",
            timeout=RUNTIME_HEALTH_TIMEOUT,
        )
        checks["llama_server"] = "ok" if 200 <= llama_health.status_code < 300 else "down"
        details["llamaHealthStatus"] = llama_health.status_code
    except httpx.HTTPError as exc:
        checks["llama_server"] = "down"
        details["llamaError"] = exc.__class__.__name__

    upstream_models: list[str] = []
    try:
        models_response = await client.get(
            f"{llama_base}/v1/models",
            timeout=RUNTIME_HEALTH_TIMEOUT,
        )
        models_response.raise_for_status()
        models_payload = models_response.json()
        upstream_models = _extract_model_ids(models_payload)
        missing = [model_id for model_id in advertised_models if model_id not in upstream_models]
        checks["model_loaded"] = "ok" if advertised_models and not missing else "down"
        details["upstreamModels"] = upstream_models
        if missing:
            details["missingModels"] = missing
    except (httpx.HTTPError, ValueError) as exc:
        checks["model_loaded"] = "down"
        details["modelError"] = exc.__class__.__name__

    if DASHBOARD_API_KEY:
        try:
            gpu_response = await client.get(
                f"{dashboard_base}/gpu",
                headers={"Authorization": f"Bearer {DASHBOARD_API_KEY}"},
                timeout=RUNTIME_HEALTH_TIMEOUT,
            )
            if 200 <= gpu_response.status_code < 300:
                gpu_payload = gpu_response.json()
                checks["gpu"] = "ok"
                details["gpu"] = {
                    "name": gpu_payload.get("name"),
                    "backend": gpu_payload.get("gpu_backend"),
                    "memoryType": gpu_payload.get("memory_type"),
                    "memoryUsedMb": gpu_payload.get("memory_used_mb"),
                    "memoryTotalMb": gpu_payload.get("memory_total_mb"),
                    "memoryPercent": gpu_payload.get("memory_percent"),
                    "utilizationPercent": gpu_payload.get("utilization_percent"),
                    "temperatureC": gpu_payload.get("temperature_c"),
                }
            else:
                checks["gpu"] = "down"
                details["gpuStatus"] = gpu_response.status_code
        except (httpx.HTTPError, ValueError) as exc:
            checks["gpu"] = "down"
            details["gpuError"] = exc.__class__.__name__
    else:
        checks["gpu"] = "unknown"
        details["gpuError"] = "dashboard_api_key_not_configured"

    if probe:
        probe_model = advertised_models[0] if advertised_models else (upstream_models[0] if upstream_models else "default")
        try:
            probe_response = await client.post(
                f"{llama_base}/v1/chat/completions",
                json={
                    "model": probe_model,
                    "messages": [{"role": "user", "content": "Reply OK only. /no_think"}],
                    "max_tokens": 8,
                    "temperature": 0,
                    "stream": False,
                },
                timeout=max(RUNTIME_HEALTH_TIMEOUT, 30),
            )
            checks["inference"] = "ok" if 200 <= probe_response.status_code < 300 else "down"
            details["inferenceStatus"] = probe_response.status_code
            details["inferenceModel"] = probe_model
        except httpx.HTTPError as exc:
            checks["inference"] = "down"
            details["inferenceError"] = exc.__class__.__name__

    status = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
    return {"status": status, "checks": checks, "details": details}


async def _hermes_status_payload(client: httpx.AsyncClient) -> dict[str, object]:
    hermes_base = HERMES_URL.rstrip("/")
    payload: dict[str, object] = {
        "status": "down",
        "hermes": "down",
        "url": hermes_base,
        "sessions": {
            "enabled": True,
            "path": HERMES_SESSIONS_PATH,
        },
    }
    try:
        response = await client.get(f"{hermes_base}/", timeout=RUNTIME_HEALTH_TIMEOUT)
        payload["statusCode"] = response.status_code
        if 200 <= response.status_code < 300:
            payload["status"] = "ok"
            payload["hermes"] = "ok"
    except httpx.HTTPError as exc:
        payload["error"] = exc.__class__.__name__
    return payload


def _extract_model_ids(payload: dict[str, object]) -> list[str]:
    model_ids: list[str] = []
    for key in ("data", "models"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for field in ("id", "model", "name"):
                value = entry.get(field)
                if isinstance(value, str) and value and value not in model_ids:
                    model_ids.append(value)
    return model_ids


def _handler_for_rule(rule_config: RouteRule) -> Callable[[Request], object]:
    if rule_config.name == "hermes_chat":
        async def hermes_handler(request: Request) -> Response:
            try:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise HermesError("json_object_required")
                config: GatewayConfig = request.app.state.config
                model = (
                    str(payload.get("model") or "").strip()
                    or (config.models[0].id if config.models else "local")
                )
                capability = next(
                    (item for item in config.capabilities if item.id == rule_config.name),
                    None,
                )
                configured_max_tokens = (
                    capability.limits.maxOutputTokens
                    if capability is not None
                    else config.limits.maxOutputTokens
                )
                requested_max_tokens = payload.get("max_tokens")
                if requested_max_tokens is None:
                    max_output_tokens = configured_max_tokens
                else:
                    try:
                        max_output_tokens = int(requested_max_tokens)
                    except (TypeError, ValueError):
                        raise HermesError("invalid_max_tokens")
                    if max_output_tokens <= 0:
                        raise HermesError("invalid_max_tokens")
                    max_output_tokens = min(max_output_tokens, configured_max_tokens)
                return StreamingResponse(
                    stream_hermes_chat(
                        payload=payload,
                        client=request.app.state.http,
                        hermes_url=HERMES_URL,
                        model=model,
                        timeout=HERMES_CHAT_TIMEOUT,
                        max_output_tokens=max_output_tokens,
                    ),
                    media_type="text/event-stream",
                )
            except HermesError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        return hermes_handler

    path = rule_config.path

    async def handler(request: Request) -> Response:
        policy: RoutePolicy = request.app.state.policy
        rule = policy.match(request.method, path)
        if not rule:
            raise HTTPException(status_code=404, detail="route_not_configured")
        return await proxy_request(
            request,
            rule,
            request.app.state.http,
            request.app.state.audit,
        )

    return handler


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

from __future__ import annotations

import os
from collections.abc import Callable
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from x402.http.middleware.fastapi import PaymentMiddlewareASGI

from .audit import AuditLog
from .config import CapabilityConfig, GatewayConfig, RouteRule, load_config
from .gateway import proxy_request
from .payments import build_resource_server, build_x402_routes
from .policy import RoutePolicy


CONFIG_PATH = os.environ.get("X402_CONFIG_PATH", "/config/config.json")
AUDIT_LOG_PATH = os.environ.get("X402_AUDIT_LOG", "/data/audit.jsonl")
PORT = int(os.environ.get("X402_GATEWAY_PORT_INTERNAL", "4020"))


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
        handler = _handler_for_rule(rule.path)
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


def _handler_for_rule(path: str) -> Callable[[Request], object]:
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

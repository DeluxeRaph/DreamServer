from __future__ import annotations

import os

import httpx
from fastapi.testclient import TestClient

os.environ.setdefault("X402_CONFIG_PATH", "../../../config/x402/config.example.json")

from app.config import GatewayConfig
from app.main import create_app


CONFIG = GatewayConfig.model_validate(
    {
        "enabled": True,
        "seller": {
            "network": "eip155:84532",
            "asset": "USDC",
            "recipient": "0x0000000000000000000000000000000000000000",
            "facilitatorUrl": "https://x402.org/facilitator",
        },
        "policy": {
            "mode": "allowlist",
            "unprotectedByDefault": True,
            "devBypass": True,
        },
        "vendor": {
            "id": "dream-test-node",
            "name": "Dream Test Node",
            "description": "Test vendor node",
            "protocolVersion": "dream-server-v1",
            "version": "0.1.0",
            "operator": {"displayName": "Tester"},
        },
        "limits": {
            "maxPromptChars": 50000,
            "maxContextItems": 20,
            "maxFileBytes": 200000,
            "maxOutputTokens": 4096,
            "supportsStreaming": True,
            "supportsFiles": False,
            "timeouts": {"defaultSeconds": 60, "maxSeconds": 300},
            "rateLimits": {"requestsPerMinute": 10, "concurrentRequests": 2},
        },
        "models": [
            {
                "id": "llama-3.1-8b-instruct-q4",
                "displayName": "Llama 3.1 8B Instruct Q4",
                "family": "llama",
                "provider": "local",
                "backend": "llama.cpp",
                "parameterCount": "8B",
                "quantization": "Q4_K_M",
                "contextWindow": 8192,
                "maxOutputTokens": 4096,
                "modalities": {"input": ["text"], "output": ["text"]},
                "hardware": {"device": "gpu", "vramGb": 16},
                "status": "available",
            }
        ],
        "capabilities": [
            {
                "id": "local_chat",
                "type": "chat.completions",
                "description": "General local LLM chat completion.",
                "path": "/v1/capabilities/local_chat",
                "method": "POST",
                "models": ["llama-3.1-8b-instruct-q4"],
                "streaming": True,
                "streamFormat": "sse",
                "pricing": {"mode": "streaming", "amount": "0.001", "currency": "USDC"},
                "requires": ["hermes", "llama-server"],
            },
            {
                "id": "coding_help",
                "type": "code.help",
                "description": "Explain, generate, or debug pasted code snippets.",
                "path": "/v1/capabilities/coding_help",
                "method": "POST",
                "models": ["llama-3.1-8b-instruct-q4"],
                "streaming": True,
                "streamFormat": "sse",
                "pricing": {"mode": "streaming", "amount": "0.003", "currency": "USDC"},
                "requires": ["hermes", "llama-server"],
            },
            {
                "id": "coding_review",
                "type": "code.review",
                "description": "Review pasted code or diffs and return findings.",
                "path": "/v1/capabilities/coding_review",
                "method": "POST",
                "models": ["llama-3.1-8b-instruct-q4"],
                "streaming": True,
                "streamFormat": "sse",
                "pricing": {"mode": "streaming", "amount": "0.005", "currency": "USDC"},
                "requires": ["hermes", "llama-server"],
            },
        ],
        "rules": [
            {
                "name": "local_chat",
                "kind": "http_route",
                "path": "/v1/capabilities/local_chat",
                "methods": ["POST"],
                "upstream": "http://llama-server:8080/v1/chat/completions",
                "price": {"mode": "streaming", "amount": "0.001", "currency": "USDC"},
            }
        ],
    }
)


class StreamingOnlyClient:
    def __init__(self) -> None:
        self.stream_called = False

    def stream(self, method: str, url: str, **kwargs: object) -> "StreamingOnlyClient":
        self.stream_called = True
        self.method = method
        self.url = url
        self.kwargs = kwargs
        return self

    async def __aenter__(self) -> "StreamingOnlyClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    @property
    def status_code(self) -> int:
        return 200

    @property
    def headers(self) -> httpx.Headers:
        return httpx.Headers({"content-type": "text/event-stream"})

    async def aiter_bytes(self):
        yield b"data: first\n\n"
        yield b"data: second\n\n"


class NoopAudit:
    def write(self, event: str, payload: dict[str, object]) -> None:
        self.event = event
        self.payload = payload


class JsonResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "http://test.local"),
                response=httpx.Response(self.status_code),
            )


class RuntimeHealthClient:
    async def get(self, url: str, **kwargs: object) -> JsonResponse:
        if url.endswith("/health"):
            return JsonResponse(200, {"status": "ok"})
        if url.endswith("/v1/models"):
            return JsonResponse(
                200,
                {
                    "data": [
                        {
                            "id": "llama-3.1-8b-instruct-q4",
                            "object": "model",
                        }
                    ]
                },
            )
        if url.endswith("/gpu"):
            return JsonResponse(
                200,
                {
                    "name": "NVIDIA Test GPU",
                    "gpu_backend": "nvidia",
                    "memory_type": "discrete",
                    "memory_used_mb": 1024,
                    "memory_total_mb": 16384,
                    "memory_percent": 6.2,
                    "utilization_percent": 7,
                    "temperature_c": 42,
                },
            )
        return JsonResponse(404, {})

    async def post(self, url: str, **kwargs: object) -> JsonResponse:
        return JsonResponse(200, {"choices": [{"message": {"content": "OK"}}]})


def client() -> TestClient:
    return TestClient(create_app(CONFIG))


def client_with_streaming_upstream() -> tuple[TestClient, StreamingOnlyClient, NoopAudit]:
    app = create_app(CONFIG)
    streaming_client = StreamingOnlyClient()
    audit = NoopAudit()
    app.state.http = streaming_client
    app.state.audit = audit
    return TestClient(app), streaming_client, audit


def test_vendor_contract_control_endpoints_are_public() -> None:
    app_client = client()

    assert app_client.get("/v1/health").status_code == 200
    assert app_client.get("/v1/health/ready").status_code == 200
    assert app_client.get("/v1/provider").status_code == 200
    assert app_client.get("/v1/vendor").status_code == 200
    assert app_client.get("/v1/models").status_code == 200
    assert app_client.get("/v1/limits").status_code == 200
    assert app_client.get("/v1/capabilities").status_code == 200


def test_provider_endpoint_advertises_marketplace_registration_contract() -> None:
    payload = client().get("/v1/provider").json()

    assert payload["id"] == "dream-test-node"
    assert payload["protocolVersion"] == "dream-server-v1"
    assert payload["providerType"] == "dream-server"
    assert payload["endpoints"] == {
        "capabilities": "/v1/capabilities",
        "health": "/v1/health",
        "runtimeHealth": "/v1/health/runtime",
        "models": "/v1/models",
        "quote": "/v1/quote",
    }


def test_models_endpoint_advertises_available_model_metadata() -> None:
    payload = client().get("/v1/models").json()

    assert payload == {
        "provider": {"id": "dream-test-node", "name": "Dream Test Node"},
        "models": [
            {
                "id": "llama-3.1-8b-instruct-q4",
                "displayName": "Llama 3.1 8B Instruct Q4",
                "family": "llama",
                "provider": "local",
                "backend": "llama.cpp",
                "parameterCount": "8B",
                "quantization": "Q4_K_M",
                "contextWindow": 8192,
                "maxOutputTokens": 4096,
                "modalities": {"input": ["text"], "output": ["text"]},
                "hardware": {"device": "gpu", "vramGb": 16},
                "status": "available",
            }
        ],
    }


def test_health_payload_uses_vendor_protocol_metadata() -> None:
    payload = client().get("/v1/health").json()

    assert payload["status"] == "ok"
    assert payload["service"] == "dream-server-x402-gateway"
    assert payload["version"] == "0.1.0"
    assert payload["protocolVersion"] == "dream-server-v1"
    assert payload["enabled"] is True


def test_readiness_reports_vendor_components() -> None:
    payload = client().get("/v1/health/ready").json()

    assert payload == {
        "status": "ok",
        "checks": {
            "api": "ok",
            "capability_registry": "ok",
            "payment_gateway": "ok",
            "payment_rules": "ok",
            "usage_metering": "ok",
        },
    }


def test_runtime_health_reports_llama_model_and_gpu(monkeypatch) -> None:
    monkeypatch.setattr("app.main.DASHBOARD_API_KEY", "secret")
    app = create_app(CONFIG)
    app.state.http = RuntimeHealthClient()

    response = TestClient(app).get("/v1/health/runtime?probe=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checks"] == {
        "api": "ok",
        "llama_server": "ok",
        "model_loaded": "ok",
        "gpu": "ok",
        "inference": "ok",
    }
    assert payload["details"]["advertisedModels"] == ["llama-3.1-8b-instruct-q4"]
    assert payload["details"]["upstreamModels"] == ["llama-3.1-8b-instruct-q4"]
    assert payload["details"]["gpu"]["name"] == "NVIDIA Test GPU"


def test_capabilities_advertise_v1_sellable_services() -> None:
    payload = client().get("/v1/capabilities").json()

    assert payload["provider"]["id"] == "dream-test-node"
    capabilities = {capability["id"]: capability for capability in payload["capabilities"]}
    assert set(capabilities) == {"local_chat", "coding_help", "coding_review"}
    assert capabilities["local_chat"]["streaming"] is True
    assert capabilities["local_chat"]["streamFormat"] == "sse"
    assert capabilities["local_chat"]["type"] == "chat.completions"
    assert capabilities["local_chat"]["method"] == "POST"
    assert capabilities["local_chat"]["models"] == ["llama-3.1-8b-instruct-q4"]
    assert capabilities["local_chat"]["requires"] == ["hermes", "llama-server"]
    assert "riskLevel" not in capabilities["local_chat"]
    assert capabilities["coding_review"]["pricing"] == {
        "amount": "0.005",
        "currency": "USDC",
        "mode": "streaming",
    }


def test_limits_advertise_streaming_and_request_bounds() -> None:
    payload = client().get("/v1/limits").json()

    assert payload["supportsStreaming"] is True
    assert payload["supportsFiles"] is False
    assert payload["maxPromptChars"] == 50000
    assert payload["timeouts"] == {"defaultSeconds": 60, "maxSeconds": 300}
    assert payload["rateLimits"] == {"requestsPerMinute": 10, "concurrentRequests": 2}


def test_quote_endpoint_returns_marketplace_quote_for_capability_model_pair() -> None:
    response = client().post(
        "/v1/quote",
        json={
            "capability": "local_chat",
            "model": "llama-3.1-8b-instruct-q4",
            "input": {"prompt": "hello"},
            "stream": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["quoteId"].startswith("quote_")
    assert payload["providerId"] == "dream-test-node"
    assert payload["capability"] == "local_chat"
    assert payload["model"] == "llama-3.1-8b-instruct-q4"
    assert payload["price"] == {"amount": "0.001", "currency": "USDC", "mode": "streaming"}
    assert payload["payment"] == {
        "protocol": "x402",
        "method": "POST",
        "resource": "/v1/capabilities/local_chat",
    }
    assert payload["streaming"] is True


def test_quote_endpoint_rejects_unknown_capability_model_pair() -> None:
    response = client().post(
        "/v1/quote",
        json={
            "capability": "local_chat",
            "model": "missing-model",
            "input": {"prompt": "hello"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "model_not_supported_for_capability"


def test_paid_capability_proxy_uses_streaming_upstream() -> None:
    app_client, upstream, audit = client_with_streaming_upstream()

    with app_client.stream(
        "POST",
        "/v1/capabilities/local_chat?trace=1",
        json={"model": "local", "messages": [{"role": "user", "content": "hello"}], "stream": True},
    ) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert body == b"data: first\n\ndata: second\n\n"
    assert upstream.stream_called is True
    assert upstream.method == "POST"
    assert upstream.url == "http://llama-server:8080/v1/chat/completions?trace=1"
    assert audit.event == "paid_request_forwarded"
    assert audit.payload["status_code"] == 200

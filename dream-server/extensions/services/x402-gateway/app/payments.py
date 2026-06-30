from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

from .cdp_auth import create_cdp_auth_headers
from .config import GatewayConfig


def _is_address(value: str) -> bool:
    return value.startswith("0x") and len(value) == 42


def _atomic_amount(amount: str, decimals: int) -> str:
    scaled = (Decimal(amount) * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN)
    if scaled <= 0:
        raise ValueError("payment amount is too small for configured asset decimals")
    return str(scaled)


def _price(amount: str, config: GatewayConfig) -> str | dict[str, object]:
    asset = config.seller.asset
    if not _is_address(asset):
        return f"${amount}"

    if config.seller.assetDecimals is None:
        raise ValueError("seller.assetDecimals is required when seller.asset is an address")

    extra: dict[str, object] = {}
    if config.seller.assetTransferMethod:
        extra["assetTransferMethod"] = config.seller.assetTransferMethod
    if config.seller.assetTransferMethod != "permit2":
        if config.seller.assetName:
            extra["name"] = config.seller.assetName
        if config.seller.assetVersion:
            extra["version"] = config.seller.assetVersion

    return {
        "amount": _atomic_amount(amount, config.seller.assetDecimals),
        "asset": asset,
        "extra": extra,
    }


def build_x402_routes(config: GatewayConfig) -> dict[str, RouteConfig]:
    routes: dict[str, RouteConfig] = {}
    for rule in config.rules:
        for method in rule.methods:
            route_key = f"{method.upper()} {rule.path}"
            routes[route_key] = RouteConfig(
                accepts=[
                    PaymentOption(
                        scheme="exact",
                        pay_to=config.seller.recipient,
                        price=_price(rule.price.amount, config),
                        network=config.seller.network,
                    ),
                ],
                mime_type=rule.metadata.mimeType,
                description=rule.metadata.description or rule.name,
            )
    return routes


def build_resource_server(config: GatewayConfig):
    facilitator_url = config.facilitator_url()
    facilitator_config: FacilitatorConfig | dict[str, object]
    if config.facilitator and config.facilitator.auth.type == "cdp_api_key":
        facilitator_config = {
            "url": facilitator_url,
            "create_headers": create_cdp_auth_headers(
                facilitator_url,
                config.facilitator.auth,
            ),
        }
    else:
        facilitator_config = FacilitatorConfig(url=facilitator_url)

    facilitator = HTTPFacilitatorClient(facilitator_config)
    server = x402ResourceServer(facilitator)
    server.register(config.seller.network, ExactEvmServerScheme())
    return server

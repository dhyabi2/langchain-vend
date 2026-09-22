"""langchain-vend: LangChain tools that call Vend API Merchant endpoints and pay in Nano (XNO) via x402.

A LangChain-compatible tool wrapping the live Vend endpoints:

    GET /api/v1/extract?url=...       0.0001 XNO  clean text/markdown from a web page
    GET /api/v1/web-search?q=...      0.0001 XNO  web search (titles, URLs, snippets)
    GET /api/v1/geoip?ip=...          0.0001 XNO  IP geolocation

Flow (the three-step x402 handshake):
  1. call the endpoint -> HTTP 402 with a payment challenge (price + destination)
  2. send that Nano amount to the quoted payTo (the swarm treasury account)
  3. retry with the resulting block hash in the X-PAYMENT header -> HTTP 200

No signup, no API keys, no subscriptions: you pay per call and only when the
server answers a 402. Price is known BEFORE any spend.

Usage (dry-run: read prices / prove the endpoint answers, spend nothing):
    from langchain_vend import VendWebSearch
    tool = VendWebSearch()               # no wallet -> _arun returns the quote
    print(await tool.ainvoke({"q": "nano cryptocurrency"}))

Usage (live): pass a wallet that can sign Nano blocks.
    from nano_pay.wallet import Wallet
    w = Wallet(seed="...")
    tool = VendWebSearch(wallet=w)
    print(await tool.ainvoke({"q": "nano cryptocurrency"}))
"""

import json
import os
from typing import Any, Dict, Optional

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

# All Vend payments settle to the swarm treasury - single destination, no issuer.
VEND_TREASURY_DEFAULT = os.environ.get(
    "VEND_PAY_TO",
    "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7",
)

VEND_ENDPOINTS = {
    "extract": ("https://extract.paypercall.dev/api/v1/extract", "0.0001"),
    "web_search": ("https://search.paypercall.dev/api/v1/web-search", "0.0001"),
    "geoip": ("https://geoip.paypercall.dev/api/v1/geoip", "0.0001"),
}


def _parse_quote(challenge: Dict[str, Any]) -> Dict[str, Any]:
    accepts = challenge.get("accepts") or []
    a0 = accepts[0] if accepts else {}
    return {
        "price_xno": challenge.get("price_xno") or a0.get("amount"),
        "asset": a0.get("asset", "XNO"),
        "network": a0.get("network", "nano:mainnet"),
        "pay_to": (a0.get("payTo") or [None])[0] if isinstance(a0.get("payTo"), list) else a0.get("payTo"),
        "scheme": a0.get("scheme", "exact"),
    }


class _VendTool(BaseTool):
    """Base: one Vend endpoint, x402 payment, dry-run safety."""

    _endpoint: str = ""
    wallet: Optional[Any] = None
    dry_run: bool = True

    def _call_vend(self, **params) -> Dict[str, Any]:
        url = self._endpoint
        resp = httpx.get(url, params=params, timeout=30)
        if resp.status_code == 402:
            challenge = resp.json()
            quote = _parse_quote(challenge)
            if not self.wallet or self.dry_run:
                raise Vend402Quote(quote)
            try:
                from nano_pay.wallet import Wallet as _W  # noqa: F401
                from nano_pay.x402 import request_with_payment
            except ImportError as e:  # pragma: no cover - live only
                raise RuntimeError("live x402 needs `pip install feeless402`") from e
            return request_with_payment(url, params=params, wallet=self.wallet, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Vend {self._endpoint} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _run(self, *args, **kwargs) -> str:
        try:
            result = self._call_vend(**kwargs)
        except Vend402Quote as q:
            return self._quote_json(q.quote)
        return json.dumps(result, ensure_ascii=False)

    async def _arun(self, *args, **kwargs) -> str:
        return self._run(*args, **kwargs)

    @staticmethod
    def _quote_json(q: Dict[str, Any]) -> str:
        return json.dumps({
            "requires_payment": True,
            "price_xno": q.get("price_xno"),
            "network": q.get("network"),
            "pay_to": q.get("pay_to"),
            "note": "Send this amount to pay_to, then retry with the block hash in X-PAYMENT.",
        })


class Vend402Quote(Exception):
    """Raised in dry-run when the endpoint would require a payment.

    Carries the parsed quote (price, pay_to, network) so an agent can decide.
    """

    def __init__(self, quote: Dict[str, Any]):
        self.quote = quote
        super().__init__(f"XNO payment required: {quote}")


class VendWebSearchInput(BaseModel):
    q: str = Field(description="The web search query.")


class VendWebSearch(_VendTool):
    """Search the web via the pay-per-call Vend web-search endpoint (Nano x402)."""
    name: str = "vend_web_search"
    description: str = "Perform a web search and return titles, URLs and snippets. Pay per call in Nano (XNO)."
    args_schema: type = VendWebSearchInput
    _endpoint: str = VEND_ENDPOINTS["web_search"][0]


class VendExtractInput(BaseModel):
    url: str = Field(description="The URL of the web page to extract clean text from.")


class VendExtract(_VendTool):
    """Extract clean text/markdown from a web page via the Vend extract endpoint (Nano x402)."""
    name: str = "vend_extract"
    description: str = "Extract clean readable text from any web page. Pay per call in Nano (XNO)."
    args_schema: type = VendExtractInput
    _endpoint: str = VEND_ENDPOINTS["extract"][0]


class VendGeoIPInput(BaseModel):
    ip: str = Field(description="The IPv4 address to geolocate.")


class VendGeoIP(_VendTool):
    """Geolocate an IP address (country, city, coordinates, ISP) via the Vend geoip endpoint (Nano x402)."""
    name: str = "vend_geoip"
    description: str = "Geolocate an IP address: country, city, coordinates, ISP. Pay per call in Nano (XNO)."
    args_schema: type = VendGeoIPInput
    _endpoint: str = VEND_ENDPOINTS["geoip"][0]


__all__ = ["VendWebSearch", "VendExtract", "VendGeoIP", "Vend402Quote", "VEND_TREASURY_DEFAULT"]

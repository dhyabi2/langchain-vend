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

Usage (live): pass a wallet that can sign Nano blocks AND opt in with
dry_run=False. A wallet on its own leaves the tool in dry-run, quoting and
spending nothing: paying is opt-in and this branch does not change that.
    from nano_pay.wallet import Wallet
    w = Wallet()                         # ~/.nano-pay/wallet.json
    w.load() if w.exists() else w.create()
    tool = VendWebSearch(wallet=w)
    print(await tool.ainvoke({"q": "nano cryptocurrency"}))
"""

import json
import os
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

# All Vend payments settle to the swarm treasury - single destination, no issuer.
# This is the account a live payment is PINNED to: `_call_vend` refuses a 402 that
# quotes any other `payTo` rather than paying it. Override it with `VEND_PAY_TO`
# when pointing these tools at a different Vend deployment.
VEND_TREASURY_DEFAULT = os.environ.get(
    "VEND_PAY_TO",
    "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7",
)

VEND_ENDPOINTS = {
    "extract": ("https://extract.paypercall.dev/api/v1/extract", "0.0001"),
    "web_search": ("https://search.paypercall.dev/api/v1/web-search", "0.0001"),
    "geoip": ("https://geoip.paypercall.dev/api/v1/geoip", "0.0001"),
}


#: 1 XNO = 10**30 raw. x402 quotes `accepts[].amount` in raw, as an integer
#: string; raw is the only exact representation, so it is what we carry.
RAW_PER_XNO = 10 ** 30


def raw_to_xno(raw: int) -> str:
    """Integer raw -> exact decimal XNO string. Never a float: at 10**30 raw to
    the XNO, binary floating point cannot hold a price exactly."""
    raw = int(raw)
    if raw < 0:
        raise ValueError("raw must not be negative")
    whole, frac = divmod(raw, RAW_PER_XNO)
    if frac == 0:
        return str(whole)
    return "%d.%s" % (whole, str(frac).zfill(30).rstrip("0"))


def _payable(accept: Any) -> bool:
    """Only the `exact` scheme on `nano:mainnet` in XNO is a price we can quote.
    A USDC offer on another network is somebody else's rail, not a cheaper XNO."""
    return (
        isinstance(accept, dict)
        and (accept.get("scheme"), accept.get("network"), accept.get("asset"))
        == ("exact", "nano:mainnet", "XNO")
    )


def _pay_to_of(accept: Dict[str, Any]) -> Optional[str]:
    pay_to = accept.get("payTo")
    if isinstance(pay_to, list):
        return (pay_to or [None])[0]
    return pay_to


def _parse_quote(challenge: Dict[str, Any]) -> Dict[str, Any]:
    """The price an agent reads before it decides to pay.

    `accepts[].amount` is integer RAW, not XNO -- reporting it as `price_xno`
    overstates the price by a factor of 10**30. The price is therefore derived
    from the raw amount of the first payable Nano offer. Vend also sends a
    convenience `price_xno` JSON number; it is used only when a payable Nano
    offer exists but carries no usable raw amount, and is rendered through
    `Decimal(str(...))` so the quote leaving this function is never a float.

    A priced quote always comes from one payable Nano offer, so `amount_raw`
    and `pay_to` describe the same offer. Without such an offer the quote is
    unpriced (`payable` False): a seller that asks for USDC on another network
    must not be handed an XNO amount to send, even when the body happens to
    carry a `price_xno` field as well.
    """
    accepts = challenge.get("accepts") or []
    offer = next((a for a in accepts if _payable(a)), None)
    if offer is not None:
        a0 = offer
    elif accepts and isinstance(accepts[0], dict):
        a0 = accepts[0]  # nothing payable: report what the seller actually asked for
    else:
        a0 = {}

    amount_raw = None
    price_xno = None
    if offer is not None:
        amount = offer.get("amount")
        if isinstance(amount, str) and amount.strip().isdigit() and int(amount) > 0:
            amount_raw = str(int(amount))
            price_xno = raw_to_xno(int(amount_raw))
        elif isinstance(amount, int) and amount > 0:
            amount_raw = str(amount)
            price_xno = raw_to_xno(amount)

    if offer is not None and price_xno is None and challenge.get("price_xno") is not None:
        # Vend's own field, in XNO. Read through str() so 0.0001 stays 0.0001.
        # Only reachable with a payable Nano offer in hand, so the amount it
        # yields belongs to the destination reported below.
        try:
            xno = Decimal(str(challenge["price_xno"]))
            raw = xno * RAW_PER_XNO
            if xno >= 0 and raw == raw.to_integral_value():
                amount_raw = str(int(raw))
                price_xno = raw_to_xno(int(amount_raw))
        except Exception:  # decimal.InvalidOperation and friends
            pass

    return {
        "price_xno": price_xno,
        "amount_raw": amount_raw,
        "asset": a0.get("asset"),
        "network": a0.get("network"),
        "pay_to": _pay_to_of(a0),
        "scheme": a0.get("scheme"),
        "payable": offer is not None and amount_raw is not None,
    }


class _VendTool(BaseTool):
    """Base: one Vend endpoint, x402 payment, dry-run safety.

    Without a wallet the tool quotes and spends nothing. With a wallet it pays
    the quoted XNO amount and returns the merchant's answer together with the
    settlement receipt: ``{"result": ..., "payment": {...}}``. Pass
    ``dry_run=True`` to hold a wallet and still only read the price.
    """

    _endpoint: str = ""
    wallet: Optional[Any] = None
    #: Nano RPC used to sign and settle; defaults to `nano_pay.rpc.RPC()`.
    rpc: Optional[Any] = None
    dry_run: bool = True

    def _call_vend(self, **params) -> Dict[str, Any]:
        url = self._endpoint
        resp = httpx.get(url, params=params, timeout=30)
        if resp.status_code == 402:
            challenge = resp.json()
            quote = _parse_quote(challenge)
            if not self.wallet or self.dry_run:
                raise Vend402Quote(quote)
            if not quote.get("amount_raw"):
                raise RuntimeError(
                    "the 402 carries no payable XNO offer; nothing was paid: "
                    + json.dumps(quote)
                )
            # The cap below is on the AMOUNT only. `request_with_payment` takes no
            # destination argument, so without this check the account the money
            # lands in is whatever the 402 names - while this package publishes a
            # single treasury and the README promises every payment settles there.
            # A quoted destination that is not that account is refused here, before
            # the wallet is handed over. `VEND_PAY_TO` moves the pin for a different
            # Vend deployment; it is not a default that can be wandered off.
            if quote.get("pay_to") != VEND_TREASURY_DEFAULT:
                raise RuntimeError(
                    "the 402 quotes a destination this package is not pinned to; "
                    "nothing was paid. expected "
                    + json.dumps(VEND_TREASURY_DEFAULT)
                    + ", quoted "
                    + json.dumps(quote.get("pay_to"))
                    + " (set VEND_PAY_TO to pay a different Vend deployment)"
                )
            try:
                from nano_pay.rpc import RPC
                from nano_pay.x402 import request_with_payment
            except ImportError as e:  # pragma: no cover - live only
                raise RuntimeError("live x402 needs `pip install feeless402`") from e
            # `request_with_payment(method, url, wallet, rpc, max_raw, **req_kwargs)`
            # returns `(response, receipt)`. The cap is exactly the raw amount
            # the agent was quoted: a seller that raises the price on the
            # paying call is refused, not paid. `timeout` is the library's.
            paid, receipt = request_with_payment(
                "GET",
                url,
                self.wallet,
                self.rpc or RPC(),
                int(quote["amount_raw"]),
                params=params,
            )
            if paid.status_code != 200:
                raise RuntimeError(
                    f"Vend {url} -> HTTP {paid.status_code} after payment: {paid.text[:200]}"
                )
            return {"result": paid.json(), "payment": receipt or {}}
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
        payable = bool(q.get("payable"))
        return json.dumps({
            "requires_payment": True,
            "payable_in_xno": payable,
            "price_xno": q.get("price_xno"),
            "amount_raw": q.get("amount_raw"),
            "asset": q.get("asset"),
            "network": q.get("network"),
            "scheme": q.get("scheme"),
            "pay_to": q.get("pay_to"),
            "note": (
                "Send exactly amount_raw (integer raw; 1 XNO = 10**30 raw) to pay_to, "
                "then retry with the block hash in X-PAYMENT."
                if payable else
                "This 402 offers no XNO payment these tools can settle: send nothing. "
                "See asset/network for the rail the seller asked for."
            ),
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


__all__ = ["VendWebSearch", "VendExtract", "VendGeoIP", "Vend402Quote", "VEND_TREASURY_DEFAULT",
           "RAW_PER_XNO", "raw_to_xno"]

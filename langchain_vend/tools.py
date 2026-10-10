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
import re
from decimal import Decimal, Inexact, localcontext
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

#: An amount in raw is ASCII decimal digits. `str.isdigit()` is NOT that test, and it is
#: what this read used: it is also true for 128 characters `int()` then REFUSES ('2' as
#: U+00B2, '4' as U+2074, the Ethiopic digits), and true for the non-ASCII decimal digits
#: `int()` happily converts (U+0663 -> 3), so an amount written in no ASCII at all could
#: price a payment. `[0-9]` and not `\d`, because `re` matches Unicode digits by default.
RAW_AMOUNT = re.compile(r"^[0-9]+$")

#: A number in SOME notation, which is what the `price_xno` fallback below exists to
#: rescue: a seller that wrote the raw amount as `1e26` or `0.0001` still named a price.
#: Text that is not a number in any notation named nothing, and is refused instead.
#: ASCII only, for the same reason as above.
NUMERIC_TEXT = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")


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
    unreadable = False
    if offer is not None:
        amount = offer.get("amount")
        if isinstance(amount, bool):
            # `bool` IS an `int` in Python, so `"amount": true` reached the integer
            # branch below, `str(True)` put the text "True" in `amount_raw`, and the
            # quote went out `payable` - after which `int(quote["amount_raw"])` on the
            # paying call raised ValueError. `true` is not an amount: nothing is priced.
            unreadable = True
        elif isinstance(amount, str):
            text = amount.strip()
            if RAW_AMOUNT.match(text):
                if int(text) > 0:
                    amount_raw = str(int(text))
                    price_xno = raw_to_xno(int(amount_raw))
            elif not NUMERIC_TEXT.match(text):
                # Not a number in any notation. This used to be `isdigit()`, which let
                # 128 such characters through to `int()` and raised ValueError out of
                # this function - on the dry-run read too, so an agent could not even
                # ask the price. An offer whose amount says nothing is priced by
                # nothing: the `price_xno` fallback below is not asked to invent one.
                unreadable = True
        elif isinstance(amount, int) and amount > 0:
            amount_raw = str(amount)
            price_xno = raw_to_xno(amount)

    if (offer is not None and price_xno is None and not unreadable
            and challenge.get("price_xno") is not None):
        # Vend's own field, in XNO. Read through str() so 0.0001 stays 0.0001.
        # Only reachable with a payable Nano offer in hand, so the amount it
        # yields belongs to the destination reported below.
        #
        # The conversion runs in its own Decimal context with `Inexact` trapped.
        # `decimal.getcontext().prec` is 28 significant digits by default and
        # this module never sets it, while an XNO amount carries up to 30
        # decimal places (10**30 raw to the XNO) and raw reaches 39 digits. So
        # `Decimal("1.000000000000000000000000000001") * RAW_PER_XNO` silently
        # returned 10**30 - one raw SHORT of the price - and the
        # `to_integral_value()` test could not see it, because a value rounded
        # at the 28th digit is still an integer. Rounding the other way is worse:
        # a price of 12.3456789012345678901234567890123 XNO yielded a cap 988 raw
        # ABOVE what was quoted.
        #
        # A price this function cannot convert exactly is now refused: the quote
        # stays unpriced, so `_call_vend` raises "no payable XNO offer" before a
        # wallet is handed over. An amount nobody quoted is never carried into
        # the spend cap.
        try:
            with localcontext() as ctx:
                ctx.prec = 60  # 39 digits of raw with headroom; no legal amount rounds
                ctx.traps[Inexact] = True
                xno = Decimal(str(challenge["price_xno"]))
                raw = xno * RAW_PER_XNO
                if xno >= 0 and raw == raw.to_integral_value():
                    amount_raw = str(int(raw))
            if amount_raw is not None:
                price_xno = raw_to_xno(int(amount_raw))
        except Exception:  # decimal.Inexact, decimal.InvalidOperation and friends
            amount_raw = None

    return {
        "price_xno": price_xno,
        "amount_raw": amount_raw,
        "asset": a0.get("asset"),
        "network": a0.get("network"),
        "pay_to": _pay_to_of(a0),
        "scheme": a0.get("scheme"),
        "payable": offer is not None and amount_raw is not None,
    }


class _PayeeMoved(RuntimeError):
    """The paying read asked for a block to an account that was never pinned."""

    def __init__(self, pinned: str, asked: str):
        self.pinned, self.asked = pinned, asked
        super().__init__(
            "the 402 moved the destination after it was checked; nothing was paid. "
            "pinned " + json.dumps(pinned) + ", asked to sign to " + json.dumps(asked)
            + " (set VEND_PAY_TO to pay a different Vend deployment)"
        )


class _PayeePinnedWallet:
    """The wallet as `request_with_payment` sees it: it may only sign to the
    account this package is pinned to.

    `_call_vend` reads the 402 itself and refuses a `payTo` that is not
    `VEND_TREASURY_DEFAULT`. But `nano_pay.x402.request_with_payment` makes its
    OWN request and signs `offer_pay_to()` of THAT read, comparing only the
    amount against `max_raw` (`nano_pay/x402.py`: `if amount > max_raw: raise
    PriceCapExceeded`, then `wallet.build_payment_block(rpc, pay_to, amount)`).
    Nothing compared the payee. So the check in `_call_vend` holds against our
    read and never against the account finally signed: an endpoint can quote the
    treasury on the read that is checked and name another account on the read
    that is paid, and the block is signed to that other account.

    `build_payment_block` is the only place the wallet signs, so refusing here
    means no block exists. Everything else is the real wallet, including `path`
    and `account()`, which `request_with_payment` reads for its journal and its
    replay proof.
    """

    def __init__(self, wallet: Any, pay_to: str):
        self._wallet = wallet
        self._pay_to = str(pay_to)

    def __getattr__(self, name):
        return getattr(self._wallet, name)

    def build_payment_block(self, rpc, to_addr, raw_amt):
        if str(to_addr) != self._pay_to:
            raise _PayeeMoved(self._pay_to, str(to_addr))
        return self._wallet.build_payment_block(rpc, to_addr, raw_amt)


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
            # `"0"` is a non-empty string, so `not quote["amount_raw"]` was False for it
            # and a quote of zero raw went on to be handed to `request_with_payment` as
            # `max_raw=0`. Zero is not a payable amount: there is nothing to send, and a
            # cap of zero either refuses with a cap nobody set ("exceeds cap 0 XNO") or,
            # if the paying read also says zero, has the wallet sign a block that moves
            # nothing. Refused here, with the same message as a 402 that priced nothing.
            if not quote.get("amount_raw") or int(quote["amount_raw"]) <= 0:
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
            # The destination is pinned again HERE, at the only moment that is
            # the real one: the block being built. The check above holds against
            # the read made by this function; `request_with_payment` re-reads the
            # 402 and signs whatever that read names. This only narrows what is
            # signed - the single account allowed is the one already checked
            # against `VEND_TREASURY_DEFAULT` - and it refuses before any block
            # exists, so a moved destination is a payment that does not happen.
            paid, receipt = request_with_payment(
                "GET",
                url,
                _PayeePinnedWallet(self.wallet, quote["pay_to"]),
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

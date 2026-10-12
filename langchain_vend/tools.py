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
    tool = VendWebSearch(wallet=w, dry_run=False)   # dry_run=False is what spends
    print(await tool.ainvoke({"q": "nano cryptocurrency"}))
"""

import json
import math
import os
import re
from decimal import Decimal, Inexact, Overflow, localcontext
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
#:
#: The fraction is one optional group, `([0-9]+(\.[0-9]*)?`, not `[0-9]+\.?[0-9]*`: with
#: the dot optional the two digit runs could split a long run of digits any of n ways, so
#: `"1" * 40000 + "x"` backtracked quadratically and took ~38 s to reject.
NUMERIC_TEXT = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$")

#: No number this module can price is anywhere near this long (a raw amount is at most
#: 39 digits; an XNO price at most 39 digits and a point). Longer text is refused before
#: any pattern or `Decimal()` reads it.
MAX_NUMERIC_TEXT = 100


def _numeric_text(text: str):
    """`NUMERIC_TEXT.match(text)`, or None for text too long to be a price."""
    if len(text) > MAX_NUMERIC_TEXT:
        return None
    return NUMERIC_TEXT.match(text)


def _exact_raw(text: str, scale: int) -> Optional[int]:
    """`text` (ASCII numeric, any notation) times `scale`, as a raw amount - or None.

    None unless the product is EXACTLY an integer with 0 < raw < 2**128. The
    arithmetic runs in its own context (precision 60, `Inexact` and `Overflow`
    trapped): the default 28 digits silently rounds a 39-digit raw amount, and a
    rounded value can still look integral, so a price could become a cap one raw
    short of, or hundreds of raw above, what was quoted.
    """
    if not _numeric_text(text):
        # `Decimal()` reads Arabic-Indic digits ("\u0661" -> 1) and underscores
        # ("1_0" -> 10); the raw amount refuses both.
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 60  # 39 digits of raw with headroom; no legal amount rounds
            ctx.traps[Inexact] = True
            ctx.traps[Overflow] = True
            raw = Decimal(text) * scale
            if 0 < raw < MAX_RAW_EXCLUSIVE and raw == raw.to_integral_value():
                return int(raw)
    except Exception:  # decimal.Inexact, Overflow, InvalidOperation and friends
        pass
    return None


#: No Nano amount is longer than this: the supply is under 2**128 raw, 39 digits.
#: Text longer than that prices nothing - and `int()` RAISES on text over 4300
#: digits, which came out of the dry-run read as ValueError.
MAX_RAW_DIGITS = len(str(2 ** 128 - 1))

#: The bound itself, applied to every path a raw amount can come from: an integer
#: amount, an integer-string amount, and the `price_xno` fallback.
MAX_RAW_EXCLUSIVE = 2 ** 128


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
    offer exists and its `amount` is missing (or null). It is rendered through
    `Decimal(str(...))` so the quote leaving this function is never a float.

    An `amount` that is present is the price, whatever notation it is in: `1e26`
    (text or a JSON number) is read exactly as 10**26 raw. A present amount that
    is not exactly a whole, positive number of raw - zero, negative, `0.5`,
    `1e-99`, Infinity, NaN, not a number at all - makes the quote unpriced: the
    `price_xno` fallback never overrules, or stands in for, a price the seller
    named in the offer.

    Every raw amount, whichever path it comes from, must be below 2**128 (the
    Nano supply bound); anything at or above it leaves the quote unpriced.

    A priced quote always comes from one payable Nano offer, so `amount_raw`
    and `pay_to` describe the same offer. Without such an offer the quote is
    unpriced (`payable` False): a seller that asks for USDC on another network
    must not be handed an XNO amount to send, even when the body happens to
    carry a `price_xno` field as well.
    """
    if not isinstance(challenge, dict):
        # A 402 whose body is a JSON array (or any other non-object) has no
        # `accepts` to read. `challenge.get` raised AttributeError straight out
        # of this function - on the dry-run read too, where the whole point is to
        # learn the price while spending nothing. A body that is not an object
        # named no offer: the quote is unpriced.
        challenge = {}
    accepts = challenge.get("accepts")
    if not isinstance(accepts, (list, tuple)):
        # x402 `accepts` is an ARRAY of offers. A seller that sent an object
        # instead (`{"0": {...}}`, which is what several JSON encoders produce
        # for a map keyed by index) reached `accepts[0]` below and raised
        # `KeyError: 0`; a number reached it and raised TypeError. Both came out
        # of the price read, so an agent could not even ask what the call costs.
        # An `accepts` that is not a list of offers carries no offer we can pay:
        # the quote is unpriced, `payable` is False, and `_call_vend` refuses
        # with "no payable XNO offer" before a wallet is handed over.
        accepts = []
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
        if isinstance(amount, bool):
            # `bool` IS an `int` in Python, so `"amount": true` reached the integer
            # branch below, `str(True)` put the text "True" in `amount_raw`, and the
            # quote went out `payable` - after which `int(quote["amount_raw"])` on the
            # paying call raised ValueError. `true` is not an amount: nothing is priced.
            pass
        elif isinstance(amount, str):
            text = amount.strip()
            numeric = _numeric_text(text)
            if RAW_AMOUNT.match(text):
                # Normalise ONCE and use the same text for the length test and for
                # `int()`: CPython's 4300-digit limit counts leading zeros, so testing
                # the stripped text and converting the unstripped one let
                # `"0" * 4300 + "1"` through to a ValueError.
                digits = text.lstrip("0") or "0"
                if len(digits) > MAX_RAW_DIGITS or int(digits) >= MAX_RAW_EXCLUSIVE:
                    # The same bound as a JSON integer: 2**128 and "9" * 39 are no
                    # more a Nano amount as text than as a number.
                    pass
                elif int(digits) > 0:
                    amount_raw = digits
                    price_xno = raw_to_xno(int(digits))
                else:
                    pass  # an explicit amount of zero prices nothing
            elif not numeric:
                # Not a number in any notation. This used to be `isdigit()`, which let
                # 128 such characters through to `int()` and raised ValueError out of
                # this function - on the dry-run read too, so an agent could not even
                # ask the price. An offer whose amount says nothing is priced by
                # nothing: the `price_xno` fallback below is not asked to invent one.
                pass
            else:
                # A number in another notation ("1e26", "+1e26", "0.5", "-5"). It is
                # the seller's price, so it is read exactly; a value that is not a
                # whole positive raw amount below 2**128 prices nothing. `price_xno`
                # used to be read instead, so "1e26" beside `price_xno: 5` quoted -
                # and capped the payment at - 5 XNO.
                raw = _exact_raw(text, 1)
                if raw is not None:
                    amount_raw = str(raw)
                    price_xno = raw_to_xno(raw)
        elif isinstance(amount, int):
            if amount >= MAX_RAW_EXCLUSIVE:
                pass  # a JSON integer no Nano amount can be
            elif amount > 0:
                amount_raw = str(amount)
                price_xno = raw_to_xno(amount)
            else:
                pass  # an explicit 0 or -3 prices nothing
        elif isinstance(amount, float):
            # JSON `Infinity` and `NaN` parse to floats; `not inf > 0` is False, so
            # Infinity used to reach the `price_xno` fallback and quote it payable.
            raw = (_exact_raw(repr(amount), 1)
                   if math.isfinite(amount) and amount > 0 else None)
            if raw is not None:  # 0.0, -1.5, 0.5, NaN and Infinity price nothing
                amount_raw = str(raw)
                price_xno = raw_to_xno(raw)
        elif amount is not None:
            pass  # a list, an object: present, and no amount at all

    if (offer is not None and offer.get("amount") is None
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
        #
        # Bounded like the raw amount: no Nano amount reaches 2**128 raw, and this
        # value becomes the spend cap on the paying call. "1e9" XNO used to quote a
        # 40-digit amount and "1e4000" a 4031-digit one. A price of zero prices
        # nothing either: it leaves `amount_raw` None like every other unpayable
        # quote, so `amount_raw is None` means "send nothing".
        raw = _exact_raw(str(challenge["price_xno"]).strip(), RAW_PER_XNO)
        if raw is not None:
            amount_raw = str(raw)
            price_xno = raw_to_xno(raw)

    return {
        "price_xno": price_xno,
        "amount_raw": amount_raw,
        "asset": a0.get("asset"),
        "network": a0.get("network"),
        "pay_to": _pay_to_of(a0),
        "scheme": a0.get("scheme"),
        # A quote of zero is not payable: the paying call refuses it, so the
        # dry-run read must not say "Send exactly amount_raw" (follow-up to #9).
        # Every path above already leaves a zero amount unpriced (`amount_raw`
        # None); the `> 0` test is kept as a backstop.
        "payable": offer is not None and amount_raw is not None and int(amount_raw) > 0,
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
            try:
                challenge = resp.json()
            except (ValueError, RecursionError):
                # A 402 whose body is not JSON at all (an HTML error page from a
                # proxy in front of the seller is the common one) priced nothing.
                # `resp.json()` raised out of the dry-run read, so the agent got a
                # JSONDecodeError where it asked for a price; it now gets an
                # unpriced quote, and the paying call refuses as it does for any
                # 402 that carries no payable XNO offer.
                challenge = {}
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

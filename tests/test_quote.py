"""The quote an agent reads before it decides to pay.

A quote is the only number the agent sees before money moves, so it has to be
the price in XNO, exactly, whatever shape the seller's 402 takes. x402 carries
the amount in `accepts[0].amount` as an INTEGER RAW string (1 XNO = 10**30
raw); Vend additionally sends a convenience `price_xno` JSON number. Reading
either one carelessly misstates the price by a factor of 10**30.
"""

import decimal
import json
import os
import sys
import time
import types
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_vend import tools
from langchain_vend.tools import _parse_quote

# A real 402 from https://search.paypercall.dev/api/v1/web-search, trimmed to
# the fields a quote is read from.
VEND_402 = {
    "error": "payment_required",
    "price_xno": 0.0001,
    "pay_to": "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7",
    "x402Version": 2,
    "resource": {"url": "https://search.paypercall.dev/api/v1/web-search"},
    "accepts": [
        {
            "scheme": "exact",
            "network": "nano:mainnet",
            "asset": "XNO",
            "amount": "100000000000000000000000000",
            "payTo": "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7",
            "maxTimeoutSeconds": 60,
        }
    ],
}

TREASURY = VEND_402["pay_to"]


def _without(d, key):
    out = json.loads(json.dumps(d))
    out.pop(key, None)
    return out


def test_price_of_the_live_vend_402_is_exactly_0_0001_xno():
    q = _parse_quote(VEND_402)
    assert q["price_xno"] == "0.0001"
    assert q["amount_raw"] == "100000000000000000000000000"
    assert q["pay_to"] == TREASURY


def test_a_standard_x402_seller_sends_only_raw_and_is_still_priced_in_xno():
    """`price_xno` is Vend's own extra field. Every other x402 seller sends
    `accepts[0].amount` alone, and raw must not be reported as XNO."""
    q = _parse_quote(_without(VEND_402, "price_xno"))
    assert q["price_xno"] == "0.0001", "raw was reported as the XNO price"
    assert q["amount_raw"] == "100000000000000000000000000"


def test_the_price_is_never_a_float():
    for challenge in (VEND_402, _without(VEND_402, "price_xno")):
        price = _parse_quote(challenge)["price_xno"]
        assert not isinstance(price, float), "an XNO amount must never be a float"
        assert isinstance(price, str)


def test_amount_raw_is_an_exact_integer_string_matching_price_xno():
    from langchain_vend.tools import RAW_PER_XNO

    q = _parse_quote(VEND_402)
    assert int(q["amount_raw"]) == int(Decimal(q["price_xno"]) * RAW_PER_XNO)


def test_raw_to_xno_is_exact_at_the_smallest_unit():
    from langchain_vend.tools import RAW_PER_XNO, raw_to_xno

    assert raw_to_xno(1) == "0.000000000000000000000000000001"
    assert raw_to_xno(RAW_PER_XNO) == "1"
    assert raw_to_xno(0) == "0"
    assert raw_to_xno(100000000000000000000000000) == "0.0001"


def test_a_402_with_no_payable_nano_offer_yields_no_price():
    q = _parse_quote({"accepts": [{"scheme": "exact", "network": "base", "asset": "USDC", "amount": "1000"}]})
    assert q["price_xno"] is None
    assert q["amount_raw"] is None


# --- a quote must not point an XNO amount at a non-Nano offer -----------------
# `price_xno` is a convenience field in the body, not an offer. Reading it when
# the 402 carries no payable Nano offer produces a quote whose amount comes
# from one rail and whose destination comes from another.

USDC_402_WITH_PRICE_XNO = {
    "error": "payment_required",
    "price_xno": 0.0001,
    "accepts": [
        {
            "scheme": "exact",
            "network": "base",
            "asset": "USDC",
            "amount": "1000",
            "payTo": "0x000000000000000000000000000000000000dead",
        }
    ],
}


def test_a_price_xno_field_does_not_make_a_usdc_402_payable_in_xno():
    q = _parse_quote(USDC_402_WITH_PRICE_XNO)
    assert q["payable"] is False
    assert q["amount_raw"] is None, "an XNO amount was quoted for a USDC-only 402"
    assert q["price_xno"] is None


def test_a_quoted_amount_always_belongs_to_the_destination_quoted_with_it():
    for challenge in (VEND_402, _without(VEND_402, "price_xno"), USDC_402_WITH_PRICE_XNO,
                      {"price_xno": 0.0001}, {}):
        q = _parse_quote(challenge)
        if q["amount_raw"] is None:
            continue
        assert (q["asset"], q["network"], q["scheme"]) == ("XNO", "nano:mainnet", "exact")
        assert q["pay_to"], "an amount to send with no destination to send it to"


def test_price_xno_with_no_accepts_at_all_yields_no_amount():
    q = _parse_quote({"price_xno": 0.0001})
    assert q["amount_raw"] is None
    assert q["pay_to"] is None
    assert q["payable"] is False


def test_price_xno_still_rescues_a_nano_offer_whose_raw_amount_is_unusable():
    """The fallback's real job: a Nano offer that carries no `amount` at all. The
    destination is the Nano offer's own, so this stays payable. (An `amount` that
    is present, in any notation, is the price itself and is never replaced.)"""
    challenge = {
        "price_xno": 0.0001,
        "accepts": [
            {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
             "payTo": TREASURY}
        ],
    }
    q = _parse_quote(challenge)
    assert q["payable"] is True
    assert q["amount_raw"] == "100000000000000000000000000"
    assert q["pay_to"] == TREASURY


def test_the_dry_run_quote_tells_an_agent_to_send_nothing_when_nothing_is_payable():
    from langchain_vend.tools import _VendTool

    rendered = json.loads(_VendTool._quote_json(_parse_quote(USDC_402_WITH_PRICE_XNO)))
    assert rendered["payable_in_xno"] is False
    assert rendered["amount_raw"] is None
    assert "send nothing" in rendered["note"].lower()
    assert "send exactly" not in rendered["note"].lower()

    payable = json.loads(_VendTool._quote_json(_parse_quote(VEND_402)))
    assert payable["payable_in_xno"] is True
    assert "send exactly" in payable["note"].lower()


# --- the README's live example has to be the one that pays -------------------

def test_readme_live_example_actually_leaves_dry_run():
    """`dry_run` defaults to True, so a wallet alone quotes and spends nothing.
    The README's 'Live (pays on the call)' block must therefore pass
    dry_run=False, or it documents a payment that never happens."""
    readme = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "README.md")).read()
    _, _, after = readme.partition("Live (pays on the call)")
    assert after, "README no longer documents a live usage"
    block = after.split("```python", 1)[1].split("```", 1)[0]
    assert "dry_run=False" in block, "the README's live example never leaves dry-run"


# --- the price_xno fallback must not round a raw amount into existence ---------
#
# `_parse_quote` derives `amount_raw` from `challenge["price_xno"]` when a payable
# Nano offer carries no usable integer `amount`. That multiplication ran in the
# process-global Decimal context, which defaults to 28 significant digits and which
# this module never sets, while an XNO price carries up to 30 decimal places
# (1 XNO = 10**30 raw) and a raw amount reaches 39 digits. The rounded product is
# still an integer, so the `to_integral_value()` test it was guarded by could not
# see the loss, and the wrong number became the spend cap `_call_vend` hands
# `request_with_payment` as `max_raw`.

def _price_only_challenge(price_xno):
    """A payable nano:mainnet offer whose `amount` is unusable, so the quote has to
    fall back to the challenge's own `price_xno` field."""
    return {
        "accepts": [{
            "scheme": "exact",
            "network": "nano:mainnet",
            "asset": "XNO",
            "payTo": "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7",
            "amount": None,
        }],
        "price_xno": price_xno,
    }


def _exact_raw(price_xno):
    """The raw amount the price really names, computed with precision to spare."""
    with decimal.localcontext() as ctx:
        ctx.prec = 120
        return decimal.Decimal(str(price_xno)) * (decimal.Decimal(10) ** 30)


def test_a_price_at_full_raw_precision_converts_exactly_and_not_one_raw_short():
    """Exactly one raw more than 1 XNO: 31 significant digits. At the default
    precision of 28 this came back as 10**30 raw -- ONE RAW SHORT of the price, and
    that short amount became the cap, so the seller's real quote exceeded it."""
    price = "1.000000000000000000000000000001"
    assert _exact_raw(price) == 10 ** 30 + 1
    q = _parse_quote(_price_only_challenge(price))
    assert q["amount_raw"] == str(10 ** 30 + 1), (
        f"quoted {q['amount_raw']} raw for a price of {price} XNO; the price names "
        f"{int(_exact_raw(price))} raw"
    )
    assert q["price_xno"] == price, "the price reported back must be the price asked"


def test_every_one_of_the_thirty_decimal_places_survives():
    """A price using all 30 decimal places Nano has. Each digit is significant, so
    any context shorter than 31 digits silently drops the tail of the amount."""
    price = "1." + "1" * 30
    q = _parse_quote(_price_only_challenge(price))
    assert q["amount_raw"] == "1" * 31
    assert q["amount_raw"] == str(int(_exact_raw(price)))


def test_a_price_finer_than_one_raw_is_refused_not_rounded_up():
    """31 decimal places names a fraction of a raw. There is nothing to send, so the
    quote stays unpriced rather than being rounded to a whole raw nobody quoted --
    this one used to come back 988 raw ABOVE the quote."""
    price = "12.3456789012345678901234567890123"
    q = _parse_quote(_price_only_challenge(price))
    assert q["amount_raw"] is None, (
        f"rounded a sub-raw price to {q['amount_raw']}, which is "
        f"{int(q['amount_raw']) - int(_exact_raw(price))} raw away from the quote"
    )
    assert q["price_xno"] is None, "an unpriced quote must not report a price"
    assert q["payable"] is False, "a quote carrying no amount must not read as payable"
    # The seller's offer is still reported back, so the caller can see what was asked.
    assert (q["network"], q["asset"], q["scheme"]) == ("nano:mainnet", "XNO", "exact")


def test_a_price_too_long_to_convert_exactly_is_refused():
    """Beyond the 60 digits the conversion is given, `Inexact` is trapped and the
    quote is refused outright rather than rounded. No legal XNO amount reaches
    here -- raw caps at 2**128-1, 39 digits -- so this is the shape of a seller
    sending something that is not an amount at all."""
    q = _parse_quote(_price_only_challenge("1" * 31 + "." + "1" * 30))
    assert q["amount_raw"] is None
    assert q["price_xno"] is None


def test_the_refusal_reaches_the_spend_path_not_just_the_quote():
    """An unpriced quote must stop the payment before a wallet is handed over."""
    class _W:
        pass

    class _R:
        status_code = 402
        text = ""

        def json(self):
            return _price_only_challenge("12.3456789012345678901234567890123")

    original = tools.httpx.get
    tools.httpx.get = lambda *a, **k: _R()
    try:
        with pytest.raises(RuntimeError, match="no payable XNO offer"):
            tools.VendWebSearch(wallet=_W(), dry_run=False).invoke({"q": "nano"})
    finally:
        tools.httpx.get = original


# --- an amount that is not an amount is refused, not crashed on ---------------
#
# `_parse_quote` read `accepts[].amount` with `amount.strip().isdigit()` and then
# `int(amount)`. Those two are not the same test. `str.isdigit()` is true for 128
# characters `int()` refuses -- '²', '⁴', the Ethiopic digits -- so the read raised
# ValueError out of this function, on the dry-run path as well, where the whole
# point is to learn the price while spending nothing. And `bool` is an `int` in
# Python, so `"amount": true` reached the integer branch, `str(True)` wrote the
# text "True" into `amount_raw`, and the quote went out `payable` with it; the
# paying call then raised ValueError on `int(quote["amount_raw"])`.
#
# Both are now refusals: the offer is unpriced, `payable` is False, and an agent
# is told to send nothing. Nothing that was payable before becomes unpayable's
# opposite -- these only ever narrow what can be paid.

def _nano_offer(amount):
    """A payable nano:mainnet offer carrying `amount`, beside a `price_xno` the
    fallback would otherwise use."""
    return {
        "price_xno": 0.0001,
        "accepts": [
            {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
             "amount": amount, "payTo": TREASURY}
        ],
    }


# U+00B2, U+2074 and U+0661 are `isdigit()`; `int()` refuses the first two and
# converts the third, so the old read either crashed or priced a payment off a
# digit that is not ASCII.
@pytest.mark.parametrize("amount", ["²", "⁴", "፩", "٣", "١٢"])
def test_an_amount_that_is_not_ascii_digits_is_refused_not_crashed_on(amount):
    q = _parse_quote(_nano_offer(amount))          # must not raise
    assert q["amount_raw"] is None, f"priced a payment off {amount!r}"
    assert q["price_xno"] is None
    assert q["payable"] is False
    # The seller's offer is still reported, so the caller can see what was asked.
    assert (q["network"], q["asset"], q["pay_to"]) == ("nano:mainnet", "XNO", TREASURY)


def test_every_isdigit_character_int_refuses_is_refused_rather_than_raising():
    """The whole class, not one example of it: `isdigit()` and `int()` disagree on
    128 characters, and each one of them used to raise out of this function."""
    disagree = [chr(c) for c in range(0x110000)
                if chr(c).isdigit() and not chr(c).isdecimal()]
    assert len(disagree) == 128, "the Unicode tables moved; re-read this test"
    for char in disagree:
        q = _parse_quote(_nano_offer(char))        # must not raise
        assert q["payable"] is False, repr(char)


@pytest.mark.parametrize("amount", [True, False])
def test_a_boolean_amount_is_refused_and_never_becomes_the_text_True(amount):
    q = _parse_quote(_nano_offer(amount))
    assert q["amount_raw"] is None, f"{amount!r} was quoted as {q['amount_raw']!r}"
    assert q["payable"] is False


def test_a_quoted_amount_is_always_an_integer_the_paying_call_can_read():
    """The consequence the crash had: `_call_vend` does `int(quote["amount_raw"])`
    to cap the payment, so an `amount_raw` that is not an integer string takes the
    agent down after the destination pin has already passed."""
    for amount in [True, False, "²", "٣", "not-a-number", "", "  "]:
        q = _parse_quote(_nano_offer(amount))
        if q["amount_raw"] is not None:
            int(q["amount_raw"])                   # must not raise


def test_an_unreadable_amount_tells_the_agent_to_send_nothing():
    from langchain_vend.tools import _VendTool

    rendered = json.loads(_VendTool._quote_json(_parse_quote(_nano_offer("²"))))
    assert rendered["payable_in_xno"] is False
    assert rendered["amount_raw"] is None
    assert "send nothing" in rendered["note"].lower()


def test_an_unreadable_amount_stops_the_payment_before_a_wallet_is_handed_over():
    class _W:
        pass

    class _R:
        status_code = 402
        text = ""

        def json(self):
            return _nano_offer(True)

    original = tools.httpx.get
    tools.httpx.get = lambda *a, **k: _R()
    try:
        with pytest.raises(RuntimeError, match="no payable XNO offer"):
            tools.VendWebSearch(wallet=_W(), dry_run=False).invoke({"q": "nano"})
    finally:
        tools.httpx.get = original


def test_prices_that_already_converted_exactly_are_unchanged():
    """The change must not cost a single quote that was already right."""
    for price, expected_raw in [
        ("0.0001", 10 ** 26),
        (0.0001, 10 ** 26),                               # the JSON number Vend sends
        ("1", 10 ** 30),
        ("0.000000000000000000000000000001", 1),           # one raw
        ("123.456", 123456 * 10 ** 27),
        ("0.000000000000000000000000000123", 123),
    ]:
        q = _parse_quote(_price_only_challenge(price))
        assert q["amount_raw"] == str(expected_raw), (
            f"{price} XNO should convert to {expected_raw} raw, got {q['amount_raw']}"
        )


def test_a_negative_price_is_still_refused():
    assert _parse_quote(_price_only_challenge("-1"))["amount_raw"] is None


def test_a_sub_raw_price_is_still_refused():
    """Below one raw there is nothing to send. This already held; it is pinned so the
    wider context cannot quietly start rounding it up to 1 raw."""
    q = _parse_quote(_price_only_challenge("0.0000000000000000000000000000001"))
    assert q["amount_raw"] is None


def test_the_context_change_does_not_leak_out_of_the_quote():
    """`localcontext` must restore the caller's context: a library that raises the
    process-global precision, or leaves `Inexact` trapped, changes arithmetic in the
    program that imported it."""
    before_prec = decimal.getcontext().prec
    before_traps = dict(decimal.getcontext().traps)
    _parse_quote(_price_only_challenge("1.000000000000000000000000000001"))
    _parse_quote(_price_only_challenge("1" * 31 + "." + "1" * 30))
    _parse_quote(_price_only_challenge("0.0001"))
    assert decimal.getcontext().prec == before_prec
    assert dict(decimal.getcontext().traps) == before_traps


# Controls: the change must not cost a single quote that already worked.

def test_an_ascii_integer_amount_is_unchanged():
    for amount, expected in [("100000000000000000000000000", "100000000000000000000000000"),
                             ("1", "1"), (100, "100"), ("007", "7")]:
        q = _parse_quote(_nano_offer(amount))
        assert q["amount_raw"] == expected, amount
        assert q["payable"] is True


def _nano_offer_priced(amount, price_xno):
    challenge = _nano_offer(amount)
    challenge["price_xno"] = price_xno
    return challenge


@pytest.mark.parametrize("amount", ["1e26", "+1e26", "1E+26", "100000000000000000000000000.0",
                                    "1.0e26", 1e26])
def test_a_whole_raw_amount_in_another_notation_is_read_exactly(amount):
    """`1e26` is the seller's price in another notation. It used to be ignored and
    `price_xno` read instead; beside `price_xno: 5` that quoted - and capped the
    payment at - 5 XNO, a price the offer never named."""
    q = _parse_quote(_nano_offer_priced(amount, "5"))
    assert q["amount_raw"] == "100000000000000000000000000", amount
    assert q["price_xno"] == "0.0001"
    assert q["payable"] is True


@pytest.mark.parametrize("amount", ["0.0001", ".5", "0.5", "1e-99", "1e999999", "1e999999999",
                                    "1.5", 0.5, 1e-99, 1e300, float("inf"), float("-inf"),
                                    "3.4028236692093846346337460743176821145600001e38"])
def test_an_amount_in_another_notation_that_is_not_a_whole_raw_amount_is_unpriced(amount):
    """A fraction of a raw, or a number past the Nano supply, prices nothing; the
    `price_xno` beside it must not stand in for it."""
    q = _parse_quote(_nano_offer_priced(amount, "5"))
    assert (q["amount_raw"], q["price_xno"], q["payable"]) == (None, None, False), amount


def test_a_json_infinity_amount_is_refused_through_invoke():
    """Python's `json` reads a bare `Infinity` as float('inf'). It used to pass the
    `> 0` test, reach the `price_xno` fallback and quote 1 XNO as payable."""
    body = ('{"price_xno": "1", "accepts": [{"scheme": "exact", "network": "nano:mainnet", '
            '"asset": "XNO", "amount": Infinity, "payTo": "%s"}]}' % TREASURY)

    class _R:
        status_code = 402
        text = body

        def json(self):
            return json.loads(body)

    original = tools.httpx.get
    tools.httpx.get = lambda *a, **k: _R()
    try:
        out = json.loads(tools.VendWebSearch().invoke({"q": "nano"}))
    finally:
        tools.httpx.get = original
    assert out["payable_in_xno"] is False
    assert out["amount_raw"] is None
    assert out["price_xno"] is None


def test_a_missing_amount_is_still_rescued_by_price_xno():
    challenge = _nano_offer(None)
    del challenge["accepts"][0]["amount"]
    q = _parse_quote(challenge)
    assert q["amount_raw"] == "100000000000000000000000000"
    assert q["payable"] is True


# -- a quote of zero raw is not a payable quote ------------------------------------------------
#
# `_call_vend`'s guard was `if not quote.get("amount_raw")`, and `"0"` is a non-empty string.


def _zero_price_challenge():
    """A payable Nano offer with no readable amount, and `price_xno: 0` alongside it.

    The `price_xno` rescue then yields `amount_raw == "0"` and `payable` True, which is the
    only way a quote of zero reaches the spend path.
    """
    challenge = json.loads(json.dumps(VEND_402))
    del challenge["accepts"][0]["amount"]
    challenge["price_xno"] = 0
    return challenge


def test_a_zero_quote_is_never_handed_to_the_paying_call():
    """`max_raw=0` is a cap nobody set.

    `request_with_payment` either refuses with "exceeds cap 0 XNO" - a message about a cap the
    agent never chose - or, when the paying read also says zero, calls
    `wallet.build_payment_block(rpc, pay_to, 0)` and signs a block that moves nothing: proof of
    work spent and a journal entry written for a payment that paid nobody.
    """
    reached = {}

    class _W:
        pass

    class _R:
        status_code = 402
        text = ""

        def json(self):
            return _zero_price_challenge()

    def _request_with_payment(method, url, wallet, rpc, max_raw, **kwargs):
        reached["max_raw"] = max_raw
        raise AssertionError(f"the spend path was reached with max_raw={max_raw!r}")

    x402 = types.ModuleType("nano_pay.x402")
    x402.request_with_payment = _request_with_payment
    rpc_mod = types.ModuleType("nano_pay.rpc")
    rpc_mod.RPC = lambda *a, **k: None

    original_get, original_mods = tools.httpx.get, {k: sys.modules.get(k) for k in
                                                    ("nano_pay.x402", "nano_pay.rpc")}
    tools.httpx.get = lambda *a, **k: _R()
    sys.modules["nano_pay.x402"], sys.modules["nano_pay.rpc"] = x402, rpc_mod
    try:
        with pytest.raises(RuntimeError, match="no payable XNO offer"):
            tools.VendWebSearch(wallet=_W(), dry_run=False).invoke({"q": "nano"})
    finally:
        tools.httpx.get = original_get
        for name, module in original_mods.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    assert "max_raw" not in reached, "a zero cap was handed to the paying call"


def test_the_zero_quote_still_reads_as_unpayable_to_the_agent():
    """A zero quote carries no amount_raw and no price, as the README says of every
    quote with `payable_in_xno` false: `amount_raw is None` means send nothing."""
    quote = _parse_quote(_zero_price_challenge())
    assert quote["amount_raw"] is None
    assert quote["price_xno"] is None
    assert quote["payable"] is False
    rendered = json.loads(tools._VendTool._quote_json(quote))
    assert rendered["payable_in_xno"] is False
    assert rendered["price_xno"] is None
    assert rendered["amount_raw"] is None
    assert "send nothing" in rendered["note"].lower()


@pytest.mark.parametrize("price", ["0", 0, 0.0, "0.0", "-0", "0e5"])
def test_every_zero_price_xno_leaves_the_quote_unpriced(price):
    q = _parse_quote(_price_only_challenge(price))
    assert (q["amount_raw"], q["price_xno"], q["payable"]) == (None, None, False)


# -- a 402 body that is not the shape x402 specifies is refused, not crashed on ------------------
#
# `_parse_quote` read `challenge.get("accepts")` and then `accepts[0]`. Neither read
# checked a type. x402 says `accepts` is an ARRAY of offers, but a seller is free to
# send whatever it likes, and what came back decided whether an agent could ask the
# price at all:
#
#   `"accepts": {"0": {...}}`   ->  KeyError: 0          (a map keyed by index: what
#                                                         several JSON encoders emit
#                                                         for a list)
#   `"accepts": 1`              ->  TypeError: 'int' object is not iterable
#   a body that is a JSON array ->  AttributeError: 'list' object has no attribute 'get'
#   a body that is not JSON     ->  json.JSONDecodeError out of `resp.json()`
#
# Each one raised out of the DRY-RUN read, where the whole point is to learn the price
# while spending nothing. A malformed 402 is now an unpriced quote: `payable` False, the
# agent told to send nothing, and the paying call refusing with the message a 402 that
# priced nothing already gets. These only ever narrow what can be paid.


def _xno_offer():
    return json.loads(json.dumps(VEND_402["accepts"][0]))


@pytest.mark.parametrize("accepts", [
    {"0": _xno_offer()},          # a map keyed by index
    {"offer": _xno_offer()},      # a map keyed by name
    {},                           # an empty map
    1,                            # a number
    1.5,
    True,
])
def test_an_accepts_that_is_not_a_list_is_refused_not_crashed_on(accepts):
    q = _parse_quote({"price_xno": 0.0001, "pay_to": TREASURY, "accepts": accepts})
    assert q["payable"] is False
    assert q["amount_raw"] is None
    assert q["price_xno"] is None


def test_a_402_body_that_is_not_a_json_object_is_refused_not_crashed_on():
    for body in ([_xno_offer()], [], "payment required", 402, None):
        q = _parse_quote(body)
        assert q["payable"] is False, repr(body)
        assert q["amount_raw"] is None


def test_a_malformed_402_never_reaches_the_paying_call():
    """A shape that used to raise KeyError now refuses where every other unpriced
    402 refuses, with no block signed and nothing handed to `request_with_payment`."""
    reached = {}

    class _W:
        pass

    class _R:
        status_code = 402
        text = ""

        def json(self):
            return {"price_xno": 0.0001, "accepts": {"0": _xno_offer()}}

    def _request_with_payment(method, url, wallet, rpc, max_raw, **kwargs):
        reached["max_raw"] = max_raw
        raise AssertionError(f"the spend path was reached with max_raw={max_raw!r}")

    x402 = types.ModuleType("nano_pay.x402")
    x402.request_with_payment = _request_with_payment
    rpc_mod = types.ModuleType("nano_pay.rpc")
    rpc_mod.RPC = lambda *a, **k: None

    original_get, original_mods = tools.httpx.get, {k: sys.modules.get(k) for k in
                                                    ("nano_pay.x402", "nano_pay.rpc")}
    tools.httpx.get = lambda *a, **k: _R()
    sys.modules["nano_pay.x402"], sys.modules["nano_pay.rpc"] = x402, rpc_mod
    try:
        with pytest.raises(RuntimeError, match="no payable XNO offer"):
            tools.VendWebSearch(wallet=_W(), dry_run=False).invoke({"q": "nano"})
    finally:
        tools.httpx.get = original_get
        for name, module in original_mods.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    assert "max_raw" not in reached


def test_a_402_whose_body_is_not_json_is_an_unpriced_quote_not_a_decode_error():
    class _R:
        status_code = 402
        text = "<html>502 Bad Gateway</html>"

        def json(self):
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    original = tools.httpx.get
    tools.httpx.get = lambda *a, **k: _R()
    try:
        rendered = json.loads(tools.VendWebSearch().invoke({"q": "nano"}))
    finally:
        tools.httpx.get = original
    assert rendered["requires_payment"] is True
    assert rendered["payable_in_xno"] is False
    assert rendered["amount_raw"] is None
    assert "send nothing" in rendered["note"]


def test_a_well_formed_402_is_untouched_by_the_type_checks():
    """The fix narrows nothing that was already payable."""
    q = _parse_quote(VEND_402)
    assert q["payable"] is True
    assert q["amount_raw"] == "100000000000000000000000000"
    assert q["pay_to"] == TREASURY
    # a tuple is a list of offers too
    q = _parse_quote({"accepts": tuple(VEND_402["accepts"])})
    assert q["payable"] is True


# Post-merge review of #10 (follow-up): three inputs still crashed the read or
# priced a payment from text that is not an ASCII amount.

_OFFER = dict(VEND_402["accepts"][0])


def test_a_raw_amount_longer_than_any_nano_balance_is_unpriced_not_a_crash():
    """`int()` refuses text over 4300 digits with ValueError, which came out of
    the dry-run read; a 4000-digit amount was quoted payable. No Nano amount is
    longer than 39 digits (2**128 - 1 raw), so anything longer prices nothing."""
    for digits in (40, 4000, 4400):
        q = _parse_quote({"accepts": [dict(_OFFER, amount="9" * digits)]})
        assert q["payable"] is False
        assert q["amount_raw"] is None
    assert _parse_quote({"accepts": [dict(_OFFER, amount=10 ** 3000)]})["payable"] is False
    biggest = str(2 ** 128 - 1)
    assert _parse_quote({"accepts": [dict(_OFFER, amount=biggest)]})["amount_raw"] == biggest


def test_a_402_body_nested_too_deep_for_json_is_an_unpriced_quote():
    class _R:
        status_code = 402

        def json(self):
            raise RecursionError("maximum recursion depth exceeded")

    original = tools.httpx.get
    tools.httpx.get = lambda *a, **k: _R()
    try:
        rendered = json.loads(tools.VendWebSearch().invoke({"q": "nano"}))
    finally:
        tools.httpx.get = original
    assert rendered["payable_in_xno"] is False
    assert rendered["amount_raw"] is None


@pytest.mark.parametrize("price", ["١", "1_0", "١.٠"])
def test_a_price_xno_that_is_not_ascii_decimal_prices_nothing(price):
    """`Decimal()` reads Arabic-Indic digits and underscores; the raw `amount`
    already refuses both. The fallback field now holds to the same rule."""
    offer = {k: v for k, v in _OFFER.items() if k != "amount"}
    q = _parse_quote({"price_xno": price, "accepts": [offer]})
    assert q["payable"] is False
    assert q["amount_raw"] is None


@pytest.mark.parametrize("price", [0, "-0", "0E-30", "0.0"])
def test_a_zero_price_xno_is_never_shown_to_the_agent_as_payable(price):
    """Follow-up to #9: the paying call refused a zero quote, but the dry-run
    read still said payable and "Send exactly amount_raw" with amount_raw "0"."""
    offer = {k: v for k, v in _OFFER.items() if k != "amount"}
    for o in (offer, dict(_OFFER, amount="0")):
        q = _parse_quote({"price_xno": price, "accepts": [o]})
        assert q["payable"] is False
        rendered = json.loads(tools._VendTool._quote_json(q))
        assert rendered["payable_in_xno"] is False
        assert "send nothing" in rendered["note"]


# -- every path to a raw amount is bounded, and none of them raises ---------------------------
#
# CPython refuses `int()` on text over 4300 digits and counts leading zeros toward that,
# so the length test and the conversion must read the same normalised text. And no
# Nano amount reaches 2**128 raw, whether it arrives as a JSON integer, as integer text
# or through the `price_xno` fallback - which becomes the spend cap on the paying call.


LEADING_ZEROS = [("0" * 4300 + "1", "1"), ("0" * 5000, None), ("0" * 4400 + "9" * 39, None)]


@pytest.mark.parametrize("amount,expected", LEADING_ZEROS, ids=["4300z+1", "5000z", "4400z+9x39"])
def test_leading_zeros_past_the_int_digit_limit_do_not_raise(amount, expected):
    """Leading zeros are not digits of the amount: `"0" * 4300 + "1"` is one raw.
    Zero is refused (an explicit zero prices nothing) and 39 nines are above 2**128."""
    q = _parse_quote(_nano_offer(amount))          # must not raise
    assert q["amount_raw"] == expected
    assert q["payable"] is (expected is not None)


@pytest.mark.parametrize("amount,expected", LEADING_ZEROS, ids=["4300z+1", "5000z", "4400z+9x39"])
def test_leading_zeros_past_the_int_digit_limit_do_not_raise_out_of_invoke(amount, expected):
    class _R:
        status_code = 402
        text = ""

        def json(self):
            return _nano_offer(amount)

    original = tools.httpx.get
    tools.httpx.get = lambda *a, **k: _R()
    try:
        out = json.loads(tools.VendWebSearch().invoke({"q": "nano"}))
    finally:
        tools.httpx.get = original
    assert out["amount_raw"] == expected
    assert out["payable_in_xno"] is (expected is not None)


def test_leading_zeros_on_a_legal_amount_still_read():
    q = _parse_quote(_nano_offer("0" * 5000 + "100000000000000000000000000"))
    assert q["amount_raw"] == "100000000000000000000000000"
    assert q["payable"] is True


@pytest.mark.parametrize("price", ["1e9", "1e4000", 1e300, "340282366.920938463463374607431768211456"])
def test_the_price_xno_fallback_is_bounded_like_a_raw_amount(price):
    q = _parse_quote(_price_only_challenge(price))
    assert q["amount_raw"] is None, f"{price!r} XNO was quoted as {q['amount_raw']}"
    assert q["price_xno"] is None
    assert q["payable"] is False


def test_the_price_xno_fallback_just_below_the_bound_still_reads():
    price = "340282366.920938463463374607431768211455"   # (2**128 - 1) raw
    q = _parse_quote(_price_only_challenge(price))
    assert q["amount_raw"] == str(2 ** 128 - 1)
    assert q["payable"] is True


@pytest.mark.parametrize("amount", [str(2 ** 128), "9" * 39, " " + str(2 ** 128) + " ",
                                    "0" + str(2 ** 128), 2 ** 128, 10 ** 39 - 1])
def test_text_and_integer_amounts_share_the_2_pow_128_bound(amount):
    q = _parse_quote(_nano_offer(amount))
    assert q["amount_raw"] is None, f"{amount!r} was quoted payable"
    assert q["payable"] is False


@pytest.mark.parametrize("amount", [str(2 ** 128 - 1), 2 ** 128 - 1])
def test_the_largest_legal_amount_is_unchanged_as_text_and_integer(amount):
    q = _parse_quote(_nano_offer(amount))
    assert q["amount_raw"] == str(2 ** 128 - 1)
    assert q["payable"] is True


@pytest.mark.parametrize("amount", ["0", "000", 0, -3, "-3", "-5", "0.0", "-1e26", "+0",
                                    "0e9", 0.0, -1.5, float("nan"), [], {}, [1]])
def test_an_explicit_non_positive_amount_is_unpriced_not_rescued(amount):
    """`_nano_offer` carries `price_xno: 0.0001` beside the offer. An offer that says
    its amount is zero or negative named a price; the fallback used to replace it with
    a different one and quote that as payable."""
    q = _parse_quote(_nano_offer(amount))
    assert q["amount_raw"] is None, f"{amount!r} was rescued as {q['amount_raw']}"
    assert q["price_xno"] is None
    assert q["payable"] is False


def test_an_explicit_zero_amount_tells_the_agent_to_send_nothing():
    from langchain_vend.tools import _VendTool

    rendered = json.loads(_VendTool._quote_json(_parse_quote(_nano_offer("0"))))
    assert rendered["payable_in_xno"] is False
    assert "send nothing" in rendered["note"].lower()


# -- a long run of digits is rejected in linear time ------------------------------------------
#
# `NUMERIC_TEXT` was `[0-9]+\.?[0-9]*`: with the dot optional, a run of n digits followed by
# something that is not a number backtracked over every split of the run between the two
# digit groups, so `"1" * 40000 + "x"` took ~38 s to reject - on the dry-run read.

LONG_NON_NUMBER = "1" * 40000 + "x"


def test_a_long_non_numeric_price_xno_is_rejected_quickly():
    start = time.perf_counter()
    q = _parse_quote(_price_only_challenge(LONG_NON_NUMBER))
    assert time.perf_counter() - start < 0.5
    assert (q["amount_raw"], q["payable"]) == (None, False)


def test_a_long_non_numeric_amount_is_rejected_quickly():
    start = time.perf_counter()
    q = _parse_quote(_nano_offer(LONG_NON_NUMBER))
    assert time.perf_counter() - start < 0.5
    assert (q["amount_raw"], q["payable"]) == (None, False)


def test_the_numeric_pattern_itself_is_linear():
    """Without the length cap in front of it, the pattern alone must still be fast."""
    start = time.perf_counter()
    assert tools.NUMERIC_TEXT.match(LONG_NON_NUMBER) is None
    assert tools.NUMERIC_TEXT.match("1" * 40000 + ".5x") is None
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("text,ok", [
    ("1e26", True), ("0.0001", True), ("+1e26", True), (".5", True), ("1.", True),
    ("-5", True), ("1.5e-3", True), ("1..5", False), (".", False), ("e5", False),
    ("1e", False), ("1_0", False), ("٣", False), ("", False), ("1.2.3", False),
])
def test_the_numeric_pattern_accepts_exactly_what_it_did(text, ok):
    assert bool(tools.NUMERIC_TEXT.match(text)) is ok


def test_numeric_text_over_the_length_cap_is_refused():
    assert tools._numeric_text("0" * 99 + "1") is not None
    assert tools._numeric_text("0" * 100 + "1") is None
    q = _parse_quote(_price_only_challenge("0" * 200 + "1"))
    assert q["amount_raw"] is None

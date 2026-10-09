"""The quote an agent reads before it decides to pay.

A quote is the only number the agent sees before money moves, so it has to be
the price in XNO, exactly, whatever shape the seller's 402 takes. x402 carries
the amount in `accepts[0].amount` as an INTEGER RAW string (1 XNO = 10**30
raw); Vend additionally sends a convenience `price_xno` JSON number. Reading
either one carelessly misstates the price by a factor of 10**30.
"""

import json
import os
import sys
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
    """The fallback's real job: a Nano offer whose `amount` is not an integer
    string. The destination is the Nano offer's own, so this stays payable."""
    challenge = {
        "price_xno": 0.0001,
        "accepts": [
            {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
             "amount": "1e26", "payTo": TREASURY}
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


# Controls: the change must not cost a single quote that already worked.

def test_an_ascii_integer_amount_is_unchanged():
    for amount, expected in [("100000000000000000000000000", "100000000000000000000000000"),
                             ("1", "1"), (100, "100"), ("007", "7")]:
        q = _parse_quote(_nano_offer(amount))
        assert q["amount_raw"] == expected, amount
        assert q["payable"] is True


def test_a_numeric_amount_in_another_notation_is_still_rescued_by_price_xno():
    """`1e26` is a number, just not an integer raw string: the seller did name a
    price, and this repository deliberately rescues it from `price_xno`
    (test_price_xno_still_rescues_a_nano_offer_whose_raw_amount_is_unusable).
    That path is untouched -- only text that names no number at all is refused."""
    for amount in ["1e26", "0.0001", "-5", "0", "+1e26", ".5"]:
        q = _parse_quote(_nano_offer(amount))
        assert q["amount_raw"] == "100000000000000000000000000", amount
        assert q["payable"] is True


def test_a_missing_amount_is_still_rescued_by_price_xno():
    challenge = _nano_offer(None)
    del challenge["accepts"][0]["amount"]
    q = _parse_quote(challenge)
    assert q["amount_raw"] == "100000000000000000000000000"
    assert q["payable"] is True

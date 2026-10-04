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

import pytest
from decimal import Decimal

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


def test_prices_that_already_converted_exactly_are_unchanged():
    """The change must not cost a single quote that was already right."""
    for price, expected_raw in [
        ("0.0001", 10 ** 26),
        (0.0001, 10 ** 26),                               # the JSON number Vend sends
        ("1", 10 ** 30),
        ("0.000000000000000000000000000001", 1),           # one raw
        ("123.456", 123456 * 10 ** 27),
        ("0.000000000000000000000000000123", 123),
        ("0", 0),
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

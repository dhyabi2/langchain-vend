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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

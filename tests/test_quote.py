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

"""The live path: a wallet means the call is paid for, in XNO, and answered.

Dry-run quoting was covered; paying was not, and nothing in the suite called
`nano_pay`'s payment entry point the way the real one is declared:

    request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
                         dry_run=False, prework=False, **req_kwargs)
                         -> (response, receipt)

The fake below mirrors that signature exactly, so a call that does not match
it fails here instead of at the moment an agent tries to spend.

No network and no wallet exist in these tests: `httpx.get` and `nano_pay` are
both replaced.
"""

import json
import sys
import types

import httpx
import pytest

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from langchain_vend.tools import RAW_PER_XNO, VendWebSearch

PRICE_RAW = "100000000000000000000000000"  # 0.0001 XNO
TREASURY = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"

VEND_402 = {
    "error": "payment_required",
    "price_xno": 0.0001,
    "pay_to": TREASURY,
    "x402Version": 2,
    "accepts": [
        {
            "scheme": "exact",
            "network": "nano:mainnet",
            "asset": "XNO",
            "amount": PRICE_RAW,
            "payTo": TREASURY,
            "maxTimeoutSeconds": 60,
        }
    ],
}

ANSWER = {"results": [{"title": "Nano (XNO)", "url": "https://nano.org"}]}
RECEIPT = {"amount_xno": "0.0001", "pay_to": TREASURY, "block": "B10C" * 16, "settled": True}


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class _Wallet:
    """Stands in for nano_pay.wallet.Wallet: only its presence is read here."""


@pytest.fixture
def paid_calls(monkeypatch):
    """Record every payment attempt; answer each one as a served 200."""
    calls = []

    def request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
                             dry_run=False, prework=False, **req_kwargs):
        calls.append({"method": method, "url": url, "wallet": wallet, "rpc": rpc,
                      "max_raw": max_raw, "req_kwargs": req_kwargs})
        return _Resp(200, ANSWER), dict(RECEIPT)

    class RPC:
        pass

    x402 = types.ModuleType("nano_pay.x402")
    x402.request_with_payment = request_with_payment
    rpc_mod = types.ModuleType("nano_pay.rpc")
    rpc_mod.RPC = RPC
    monkeypatch.setitem(sys.modules, "nano_pay", types.ModuleType("nano_pay"))
    monkeypatch.setitem(sys.modules, "nano_pay.x402", x402)
    monkeypatch.setitem(sys.modules, "nano_pay.rpc", rpc_mod)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, VEND_402))
    return calls


def test_a_wallet_pays_the_quote_and_returns_the_answer(paid_calls):
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano cryptocurrency"}))
    assert len(paid_calls) == 1, "a tool holding a wallet never attempted the payment"
    assert out["result"] == ANSWER, "the paid-for answer did not reach the caller"
    assert out["payment"]["block"] == RECEIPT["block"], "the settlement receipt was dropped"


def test_the_payment_call_matches_the_nano_pay_entry_point(paid_calls):
    VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano cryptocurrency"})
    call = paid_calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://search.paypercall.dev/api/v1/web-search"
    assert call["rpc"] is not None, "no Nano RPC was supplied; the payment cannot settle"
    assert call["req_kwargs"]["params"] == {"q": "nano cryptocurrency"}


def test_the_spend_cap_is_the_exact_raw_amount_quoted(paid_calls):
    VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano cryptocurrency"})
    cap = paid_calls[0]["max_raw"]
    assert isinstance(cap, int) and not isinstance(cap, bool), "a cap on an XNO amount must be integer raw"
    assert cap == int(PRICE_RAW) == RAW_PER_XNO // 10_000


def test_no_wallet_still_quotes_and_spends_nothing(paid_calls):
    out = json.loads(VendWebSearch().invoke({"q": "nano cryptocurrency"}))
    assert out["requires_payment"] is True
    assert out["price_xno"] == "0.0001"
    assert paid_calls == [], "a wallet-less tool must never reach the payment path"


def test_dry_run_holds_a_wallet_without_spending(paid_calls):
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=True).invoke({"q": "nano"}))
    assert out["requires_payment"] is True
    assert paid_calls == [], "dry_run=True must not spend"


def test_a_402_with_no_payable_xno_offer_is_refused_not_paid(paid_calls, monkeypatch):
    usdc_only = {"accepts": [{"scheme": "exact", "network": "base", "asset": "USDC", "amount": "1000"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, usdc_only))
    with pytest.raises(RuntimeError, match="no payable XNO offer"):
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})
    assert paid_calls == [], "paid against a 402 that quoted no XNO amount"

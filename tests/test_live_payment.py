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

from langchain_vend import tools
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


# --- the destination the money lands in ---------------------------------------
#
# `max_raw` caps the AMOUNT. Nothing capped the DESTINATION:
# `nano_pay.x402.request_with_payment` is declared
# `(method, url, wallet, rpc, max_raw, headers=None, dry_run=False, prework=False,
# **req_kwargs)` - no destination parameter - so the account a live payment lands
# in was whatever the 402 named, while `VEND_TREASURY_DEFAULT` sat exported and
# unread and README.md said "All payments settle to the published swarm treasury
# account". These tests hold the pin that makes that sentence true.

OTHER_ACCOUNT = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"

FOREIGN_402 = json.loads(json.dumps(VEND_402))
FOREIGN_402["accepts"][0]["payTo"] = OTHER_ACCOUNT
FOREIGN_402["pay_to"] = OTHER_ACCOUNT


def test_a_402_quoting_a_destination_we_are_not_pinned_to_is_refused_not_paid(
    paid_calls, monkeypatch
):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, FOREIGN_402))
    with pytest.raises(RuntimeError, match="not pinned to"):
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})
    assert paid_calls == [], (
        "paid 0.0001 XNO to an account the 402 chose: the cap is on the amount, "
        "not on where the money goes"
    )


def test_the_refusal_names_both_accounts_so_the_agent_can_tell_them_apart(
    paid_calls, monkeypatch
):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, FOREIGN_402))
    with pytest.raises(RuntimeError) as e:
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})
    assert TREASURY in str(e.value) and OTHER_ACCOUNT in str(e.value)


def test_a_402_with_a_payable_amount_but_no_destination_is_refused(
    paid_calls, monkeypatch
):
    no_dest = json.loads(json.dumps(VEND_402))
    no_dest["accepts"][0].pop("payTo")
    no_dest.pop("pay_to")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, no_dest))
    with pytest.raises(RuntimeError, match="not pinned to"):
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})
    assert paid_calls == [], "paid an amount with no destination quoted for it"


# --- controls: the pin must not cost a good payment ---------------------------


def test_the_real_vend_treasury_quote_is_still_paid(paid_calls):
    """The recorded live 402 names exactly VEND_TREASURY_DEFAULT. If the pin
    refused this, it would refuse every real call."""
    out = json.loads(
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})
    )
    assert len(paid_calls) == 1 and out["result"] == ANSWER
    assert VEND_402["accepts"][0]["payTo"] == tools.VEND_TREASURY_DEFAULT


def test_vend_pay_to_moves_the_pin_rather_than_removing_it(paid_calls, monkeypatch):
    """A different Vend deployment is reachable - by naming its account, not by
    accepting any account."""
    monkeypatch.setattr(tools, "VEND_TREASURY_DEFAULT", OTHER_ACCOUNT)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, FOREIGN_402))
    out = json.loads(
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})
    )
    assert len(paid_calls) == 1 and out["result"] == ANSWER
    # and the original treasury is now the one refused, so the pin really moved
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, VEND_402))
    with pytest.raises(RuntimeError, match="not pinned to"):
        VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"})


def test_the_pin_does_not_touch_dry_run_quoting(paid_calls, monkeypatch):
    """A quote still reports a foreign destination instead of refusing: reading a
    price spends nothing, and seeing the odd account is how an agent finds out."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, FOREIGN_402))
    out = json.loads(VendWebSearch().invoke({"q": "nano"}))
    assert out["requires_payment"] is True and out["pay_to"] == OTHER_ACCOUNT
    assert paid_calls == []

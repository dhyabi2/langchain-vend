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


# --- the destination at SIGNING time, not only at the quote -------------------
#
# The pin above is checked against the 402 that `_call_vend` reads itself.
# `nano_pay.x402.request_with_payment` makes its OWN request and signs the payee
# of THAT read:
#
#     r = requests.request(method, url, ...)          # a second read of the 402
#     quote = parse_quote(r); offer = pick_nano_offer(quote)
#     amount = offer_amount_raw(offer); pay_to = offer_pay_to(offer)
#     if amount > max_raw: raise PriceCapExceeded(...)   # the amount is checked
#     block, ... = wallet.build_payment_block(rpc, pay_to, amount)   # pay_to is not
#
# So an endpoint can quote the treasury on the read that is checked and name
# another account on the read that is paid. Every fake above replaces
# `request_with_payment` wholesale and never re-reads, so nothing in the suite
# could see it. The fake below mirrors that body instead.

SECOND_READ_402 = json.loads(json.dumps(VEND_402))
SECOND_READ_402["accepts"][0]["payTo"] = OTHER_ACCOUNT
SECOND_READ_402["pay_to"] = OTHER_ACCOUNT


class _SigningWallet:
    """A wallet that records every block it is asked to sign."""

    path = "/nonexistent/wallet.json"

    def __init__(self):
        self.signed = []

    def build_payment_block(self, rpc, to_addr, raw_amt):
        self.signed.append((str(to_addr), int(raw_amt)))
        return {"type": "state", "link_as_account": str(to_addr)}, "B10C" * 16, "work-root"

    def account(self):
        raise AssertionError("the replay proof is not part of this test")

    def payment_succeeded(self, *a, **k):
        pass

    def payment_failed(self, *a, **k):
        pass


@pytest.fixture
def rereading_payer(monkeypatch):
    """`request_with_payment` as feeless402 really declares it: it re-reads the
    402 and signs the payee of that second read.

    `reads` is the queue of 402 bodies: the first is what `_call_vend` checks,
    the second is what the payment actually signs against.
    """
    reads = []

    def httpx_get(*a, **k):
        return _Resp(402, reads.pop(0) if len(reads) > 1 else reads[0])

    def request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
                             dry_run=False, prework=False, **req_kwargs):
        body = httpx_get()                      # the payer's own read of the 402
        offer = body.json()["accepts"][0]
        amount = int(offer["amount"])
        pay_to = offer.get("payTo")
        if amount > max_raw:
            raise RuntimeError("PriceCapExceeded")
        wallet.build_payment_block(rpc, pay_to, amount)
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
    monkeypatch.setattr(httpx, "get", httpx_get)
    return reads


def test_a_destination_moved_after_the_check_is_refused_at_signing(rereading_payer):
    """Quote the treasury, sign to somebody else: nothing may be signed.

    Without the pin at signing time this passes 0.0001 XNO to OTHER_ACCOUNT and
    returns a served 200, because the amount is right, the cap is satisfied and
    the account is the only thing nobody compared.
    """
    rereading_payer[:] = [VEND_402, SECOND_READ_402]
    wallet = _SigningWallet()
    with pytest.raises(RuntimeError, match="moved the destination"):
        VendWebSearch(wallet=wallet, dry_run=False).invoke({"q": "nano"})
    assert wallet.signed == [], (
        "signed a block to %r: the destination was checked against the first read "
        "of the 402 and never against the account the block pays" % (wallet.signed,)
    )


def test_the_refusal_names_the_pinned_and_the_asked_account(rereading_payer):
    rereading_payer[:] = [VEND_402, SECOND_READ_402]
    with pytest.raises(RuntimeError) as e:
        VendWebSearch(wallet=_SigningWallet(), dry_run=False).invoke({"q": "nano"})
    assert TREASURY in str(e.value) and OTHER_ACCOUNT in str(e.value)
    assert "nothing was paid" in str(e.value)


def test_an_unchanged_destination_is_still_signed_and_paid(rereading_payer):
    """The control: the pin must not cost the ordinary payment. Both reads name
    the treasury, which is what a real Vend endpoint does."""
    rereading_payer[:] = [VEND_402, VEND_402]
    wallet = _SigningWallet()
    out = json.loads(VendWebSearch(wallet=wallet, dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == ANSWER
    assert wallet.signed == [(TREASURY, int(PRICE_RAW))]


def test_a_payee_restated_identically_on_the_paying_read_is_still_paid(rereading_payer):
    """A second read that is a different dict object with the same account is the
    same payee: the pin compares addresses, not identity."""
    restated = json.loads(json.dumps(VEND_402))
    rereading_payer[:] = [VEND_402, restated]
    wallet = _SigningWallet()
    out = json.loads(VendWebSearch(wallet=wallet, dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == ANSWER and wallet.signed == [(TREASURY, int(PRICE_RAW))]


def test_vend_pay_to_still_moves_the_pin_at_signing_time(rereading_payer, monkeypatch):
    """A different Vend deployment stays reachable: the pin follows the operator's
    account, it is not a hardcoded single address."""
    monkeypatch.setattr(tools, "VEND_TREASURY_DEFAULT", OTHER_ACCOUNT)
    rereading_payer[:] = [SECOND_READ_402, SECOND_READ_402]
    wallet = _SigningWallet()
    out = json.loads(VendWebSearch(wallet=wallet, dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == ANSWER and wallet.signed == [(OTHER_ACCOUNT, int(PRICE_RAW))]


def test_the_pinned_wallet_forwards_everything_else_to_the_real_wallet():
    """`request_with_payment` reads `wallet.path` for its journal and calls
    `payment_succeeded`/`payment_failed`. A proxy that hid those would break the
    payment it is meant to protect."""
    inner = _SigningWallet()
    proxied = tools._PayeePinnedWallet(inner, TREASURY)
    assert proxied.path == inner.path
    proxied.payment_failed("work-root")        # must not raise
    assert proxied.build_payment_block(None, TREASURY, 1)[1] == "B10C" * 16
    assert inner.signed == [(TREASURY, 1)]


def test_nothing_is_signed_when_the_amount_is_raised_on_the_paying_read(rereading_payer):
    """The amount half of the same hole, already closed by `max_raw`: pinned here
    so a later change cannot pass the operator's cap instead of the quote."""
    dearer = json.loads(json.dumps(VEND_402))
    dearer["accepts"][0]["amount"] = str(int(PRICE_RAW) + 1)
    rereading_payer[:] = [VEND_402, dearer]
    wallet = _SigningWallet()
    with pytest.raises(RuntimeError, match="PriceCapExceeded"):
        VendWebSearch(wallet=wallet, dry_run=False).invoke({"q": "nano"})
    assert wallet.signed == []


# ── the seller took the money and did not serve the call ─────────────────────
#
# `request_with_payment` returns `(response, receipt)` and the payment has
# ALREADY settled by the time it returns. So a non-200 here is not a failed
# call: it is a paid call that was not answered, and the receipt is the only
# record an agent has that its XNO left the wallet. These tests pin that the
# record survives the failure.


@pytest.fixture
def paid_then_refused(monkeypatch):
    """Settle the payment, then answer the paid call with the given status."""
    state = {"status": 502, "payload": None, "receipt": dict(RECEIPT)}

    def request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
                             dry_run=False, prework=False, **req_kwargs):
        resp = _Resp(state["status"], state["payload"])
        resp.text = "upstream unavailable"
        return resp, state["receipt"]

    x402 = types.ModuleType("nano_pay.x402")
    x402.request_with_payment = request_with_payment
    rpc_mod = types.ModuleType("nano_pay.rpc")
    rpc_mod.RPC = type("RPC", (), {})
    monkeypatch.setitem(sys.modules, "nano_pay", types.ModuleType("nano_pay"))
    monkeypatch.setitem(sys.modules, "nano_pay.x402", x402)
    monkeypatch.setitem(sys.modules, "nano_pay.rpc", rpc_mod)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, VEND_402))
    return state


def test_a_paid_call_the_seller_refused_keeps_the_settlement_receipt(paid_then_refused):
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(tools.VendPaidButNotServed) as caught:
        tool.invoke({"q": "nano cryptocurrency"})
    err = caught.value
    assert err.receipt == RECEIPT, "the settlement receipt was dropped on a paid call"
    assert err.block == RECEIPT["block"], (
        "the block hash that moved the XNO was lost; an agent cannot retry "
        "X-PAYMENT or prove it paid"
    )
    assert err.amount_raw == PRICE_RAW, "the amount paid was not reported"
    assert err.status_code == 502
    assert err.url == "https://search.paypercall.dev/api/v1/web-search"


def test_the_block_hash_is_in_the_message_an_agent_actually_reads(paid_then_refused):
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(tools.VendPaidButNotServed) as caught:
        tool.invoke({"q": "nano cryptocurrency"})
    text = str(caught.value)
    assert RECEIPT["block"] in text, "the one irreplaceable fact is not in the error text"
    assert PRICE_RAW in text
    assert "502" in text and "upstream unavailable" in text


def test_it_is_still_a_runtime_error_so_existing_handlers_keep_working(paid_then_refused):
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(RuntimeError):
        tool.invoke({"q": "nano cryptocurrency"})


def test_a_receipt_with_no_block_reports_none_rather_than_inventing_one(paid_then_refused):
    paid_then_refused["receipt"] = {"settled": False}
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(tools.VendPaidButNotServed) as caught:
        tool.invoke({"q": "nano cryptocurrency"})
    assert caught.value.block is None
    assert caught.value.receipt == {"settled": False}, "the receipt we did get is still reported"


def test_no_receipt_at_all_is_an_empty_dict_not_a_crash(paid_then_refused):
    paid_then_refused["receipt"] = None
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(tools.VendPaidButNotServed) as caught:
        tool.invoke({"q": "nano cryptocurrency"})
    assert caught.value.receipt == {}
    assert caught.value.block is None


def test_a_served_200_is_untouched_by_any_of_this(paid_calls):
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == ANSWER
    assert out["payment"]["block"] == RECEIPT["block"]

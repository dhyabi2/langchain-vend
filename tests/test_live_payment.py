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
# `request_with_payment` returns `(response, receipt)` once a signed block has
# been handed to the seller. Whether it settled is the receipt's `settled`:
# True (on the ledger: the XNO was spent), False (refused and not on the
# ledger: nothing was paid) or "indeterminate" (unknown: check the block on
# the ledger first). The receipt is the only record an agent has of that block,
# and these tests pin that it survives the failure and that the message says
# what the receipt says. Calling the tool again can sign a NEW payment; the
# tool has no way to re-present the block, so recovery is quoting it to the
# seller, never a free retry.


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
        "the block hash that moved the XNO was lost; an agent cannot quote "
        "it to the seller or prove it paid"
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
    assert caught.value.signed is None
    assert caught.value.settled == "indeterminate"


def test_no_receipt_at_all_is_an_empty_dict_not_a_crash(paid_then_refused):
    paid_then_refused["receipt"] = None
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(tools.VendPaidButNotServed) as caught:
        tool.invoke({"q": "nano cryptocurrency"})
    assert caught.value.receipt == {}
    assert caught.value.block is None


def test_no_receipt_says_no_payment_was_signed_and_names_no_null_block(paid_then_refused):
    # feeless402 returns (response, None) without signing when the paying read
    # is not a 402: nothing was paid, and there is no block to check.
    err = _refused(paid_then_refused, None)
    assert err.signed is False and err.settled is False
    text = str(err)
    assert "no payment was signed for this call" in text
    assert "null" not in text and "None" not in text
    assert "ledger" not in text and "unknown" not in text and "spent" not in text
    assert text.startswith(
        "Vend https://search.paypercall.dev/api/v1/web-search -> HTTP 502 after payment: "
        "upstream unavailable -- ")


def test_a_served_200_is_untouched_by_any_of_this(paid_calls):
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == ANSWER
    assert out["payment"]["block"] == RECEIPT["block"]


def _refused(paid_then_refused, receipt):
    paid_then_refused["receipt"] = receipt
    tool = VendWebSearch(wallet=_Wallet(), dry_run=False)
    with pytest.raises(tools.VendPaidButNotServed) as caught:
        tool.invoke({"q": "nano cryptocurrency"})
    return caught.value


def test_settled_true_says_the_payment_settled(paid_then_refused):
    err = _refused(paid_then_refused, dict(RECEIPT, ledger="confirmed"))
    assert err.settled is True and err.ledger == "confirmed"
    text = str(err)
    assert "settled and the XNO was spent" in text
    assert "can sign a NEW payment" in text
    assert "quote this block to the seller" in text


def test_settled_true_frames_the_library_re_present_note(paid_then_refused):
    note = ("payment is on the ledger; if the merchant did not deliver, "
            "re-present this SAME block - do not pay again")
    err = _refused(paid_then_refused, dict(RECEIPT, ledger="confirmed", note=note))
    assert err.note == note
    text = str(err)
    assert "(for callers that can re-present a block; this tool cannot): " + note in text
    assert "this tool cannot re-present a block" in text
    assert text.index(note) < text.index("can sign a NEW payment")


def test_settled_false_says_nothing_was_paid(paid_then_refused):
    note = "merchant refused and the ledger does not hold the block; nothing was paid"
    err = _refused(paid_then_refused, dict(RECEIPT, settled=False, ledger="absent", note=note))
    assert err.settled is False and err.ledger == "absent" and err.note == note
    text = str(err)
    assert "nothing was paid" in text and note in text
    assert "spent" not in text and "settled and" not in text


def test_indeterminate_says_unknown_and_to_check_the_ledger(paid_then_refused):
    note = ("no confirmation from merchant or ledger; check this block hash "
            "before paying again - do NOT re-pay blind")
    err = _refused(paid_then_refused, dict(RECEIPT, settled="indeterminate",
                                           ledger="unreachable", note=note))
    assert err.settled == "indeterminate" and err.ledger == "unreachable"
    text = str(err)
    assert "unknown" in text and "on the ledger before anything else" in text
    assert note in text
    assert "spent" not in text and "nothing was paid" not in text


def test_a_receipt_without_settled_is_treated_as_unknown(paid_then_refused):
    err = _refused(paid_then_refused, {"block": RECEIPT["block"]})
    assert err.settled == "indeterminate"
    assert err.ledger is None and err.note is None
    assert "unknown" in str(err) and "spent" not in str(err)


def test_the_old_after_payment_text_still_matches(paid_then_refused):
    err = _refused(paid_then_refused, dict(RECEIPT))
    text = str(err)
    assert text.startswith(
        "Vend https://search.paypercall.dev/api/v1/web-search -> HTTP 502 after payment: ")


@pytest.mark.parametrize("receipt", [dict(RECEIPT), dict(RECEIPT, settled=False),
                                     dict(RECEIPT, settled="indeterminate"), None])
def test_the_seller_body_comes_right_after_the_old_prefix(paid_then_refused, receipt):
    import re

    m = re.search(r"HTTP (\d+) after payment: (.*)", str(_refused(paid_then_refused, receipt)))
    assert m and m.group(1) == "502"
    assert m.group(2).startswith("upstream unavailable -- ")


def test_the_seller_body_is_still_cut_to_200_characters(paid_then_refused):
    err = tools.VendPaidButNotServed("https://x", 500, "z" * 500, RECEIPT, PRICE_RAW)
    assert err.body == "z" * 200
    assert "after payment: " + "z" * 200 + " -- " in str(err)
    assert "z" * 201 not in str(err)


@pytest.mark.parametrize("settled", [True, False, "indeterminate", None])
def test_the_message_never_says_to_put_the_block_in_x_payment(paid_then_refused, settled):
    receipt = dict(RECEIPT)
    if settled is None:
        receipt.pop("settled")
    else:
        receipt["settled"] = settled
    text = str(_refused(paid_then_refused, receipt))
    assert "X-PAYMENT" not in text.upper()
    assert "retry" not in text.lower()


@pytest.mark.parametrize("settled", [True, False, "indeterminate"])
def test_the_exception_survives_pickle_and_copy(settled):
    import copy
    import pickle

    receipt = dict(RECEIPT, settled=settled, ledger="absent", note="n")
    err = tools.VendPaidButNotServed("https://x/y", 502, "nope", receipt, PRICE_RAW)
    names = ("url", "status_code", "body", "receipt", "block", "signed", "amount_raw",
             "settled", "ledger", "note", "args")
    for clone in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
        assert type(clone) is tools.VendPaidButNotServed
        for name in names:
            assert getattr(clone, name) == getattr(err, name), name
        assert str(clone) == str(err)


def test_notes_and_caller_set_attributes_survive_pickle_and_copy():
    import copy
    import pickle

    err = tools.VendPaidButNotServed("https://x/y", 502, "nope", dict(RECEIPT), PRICE_RAW)
    # `BaseException.add_note` is PEP 678, Python 3.11+. pyproject declares
    # `requires-python = ">=3.10"` and CI runs a 3.10 leg, where calling it
    # raises AttributeError before this test asserts anything - which is how it
    # failed: `'VendPaidButNotServed' object has no attribute 'add_note'`, on
    # 3.10 only, with 3.11/3.12/3.13 green. There is no `__notes__` to carry on
    # 3.10, so that half of the claim is checked where the API exists; an
    # attribute a caller sets is carried on every version and is checked on all
    # of them. Both halves need the third element of `__reduce__`.
    has_notes = hasattr(err, "add_note")
    if has_notes:
        err.add_note("while searching for nano")
    err.request_id = "abc"
    for clone in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
        if has_notes:
            assert clone.__notes__ == ["while searching for nano"]
        assert clone.request_id == "abc"
        assert clone.block == RECEIPT["block"] and str(clone) == str(err)


@pytest.mark.parametrize("receipt", [
    {"settled": True, "block": ""},
    {"settled": True, "ledger": "confirmed"},
    {"settled": "indeterminate", "block": b"AB"},
    ("custom", "wallet", "receipt"),
])
def test_a_receipt_that_may_have_paid_is_never_told_nothing_was_paid(paid_then_refused, receipt):
    err = _refused(paid_then_refused, receipt)
    text = str(err)
    assert "nothing was paid" not in text
    assert "no payment was signed" not in text
    assert err.settled is not False
    assert err.settled == "indeterminate"
    assert err.signed is not False
    assert "may have been signed" in text or "unknown" in text
    assert "can sign a NEW payment" in text


def test_a_bytes_block_is_rendered_not_crashed_on(paid_then_refused):
    err = _refused(paid_then_refused, {"settled": "indeterminate", "block": b"AB"})
    assert err.block == "AB" and err.signed is True
    assert '"AB"' in str(err)


def test_a_non_dict_receipt_survives_pickle_and_copy():
    import copy
    import pickle

    for receipt in (None, ("custom",), {"settled": True, "block": ""}):
        err = tools.VendPaidButNotServed("https://x/y", 502, "nope", receipt, PRICE_RAW)
        for clone in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
            assert str(clone) == str(err)
            assert (clone.signed, clone.settled, clone.receipt) == (err.signed, err.settled, err.receipt)


# ── the seller served the paid call, but not as JSON ─────────────────────────
#
# A paid 200 is the seller's answer to a call that was already paid for. When
# its body is not JSON (plain text, an HTML page, nothing at all) `paid.json()`
# raised out of the tool and took the receipt with it: the agent had paid,
# held no block hash to show for it, and calling again signs a NEW payment.
# The served text is now returned, bounded, alongside the same receipt a JSON
# answer gets.


@pytest.fixture
def paid_non_json(monkeypatch):
    """Settle the payment, then serve the paid call as a real httpx 200."""
    state = {"response": httpx.Response(200, text="plain text, not JSON"),
             "receipt": dict(RECEIPT)}

    def request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
                             dry_run=False, prework=False, **req_kwargs):
        return state["response"], state["receipt"]

    x402 = types.ModuleType("nano_pay.x402")
    x402.request_with_payment = request_with_payment
    rpc_mod = types.ModuleType("nano_pay.rpc")
    rpc_mod.RPC = type("RPC", (), {})
    monkeypatch.setitem(sys.modules, "nano_pay", types.ModuleType("nano_pay"))
    monkeypatch.setitem(sys.modules, "nano_pay.x402", x402)
    monkeypatch.setitem(sys.modules, "nano_pay.rpc", rpc_mod)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(402, VEND_402))
    return state


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="plain text, not JSON"),
    httpx.Response(200, html="<html><body>served</body></html>"),
    httpx.Response(200, content=b""),
    httpx.Response(200, content=b"\xff\xfe\x00garbage",
                   headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"[" * 100000),
], ids=["text", "html", "empty", "undecodable", "too-deep"])
def test_a_paid_non_json_200_still_returns_the_receipt(paid_non_json, response):
    paid_non_json["response"] = response
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out["payment"] == RECEIPT, (
        "the call was paid and served, and the receipt was lost with the body")
    assert out["payment"]["block"] == RECEIPT["block"]
    assert out["result_is_json"] is False
    assert isinstance(out["result"], str)


def test_the_served_text_is_returned_as_the_result(paid_non_json):
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == "plain text, not JSON"
    assert out["result_truncated"] is False


def test_the_served_text_is_bounded(paid_non_json):
    limit = tools.MAX_NON_JSON_RESULT
    paid_non_json["response"] = httpx.Response(200, text="x" * (limit * 3))
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out["result"] == "x" * limit
    assert out["result_truncated"] is True
    assert out["payment"]["block"] == RECEIPT["block"]


def test_no_receipt_on_a_non_json_200_is_an_empty_dict_not_a_crash(paid_non_json):
    paid_non_json["receipt"] = None
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out["payment"] == {}
    assert out["result"] == "plain text, not JSON"


def test_a_json_200_keeps_its_shape(paid_non_json):
    paid_non_json["response"] = httpx.Response(200, json=ANSWER)
    out = json.loads(VendWebSearch(wallet=_Wallet(), dry_run=False).invoke({"q": "nano"}))
    assert out == {"result": ANSWER, "payment": RECEIPT}

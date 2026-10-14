# langchain-vend — code audit, 2026-10-14

Previous audit 2026-10-12 (`42cbcb5`, a malformed 402 container). HEAD audited: `main` at
`d3f1d4e` ("Quote read: a 4301-digit amount crashed it; zero and non-ASCII prices read as
payable (#11)"). Read under one question: can an agent pay in XNO with this today without
being hurt?

Baseline on `main`, the three legs CI runs, before any change:

```
$ python -m compileall -q langchain_vend tests examples      # clean
$ pip install -e ".[test]" && pytest -q                      # 233 passed
$ pip install feeless402 && python .github/dependency_signature_check.py
  OK: langchain_vend's live payment call binds to feeless402 0.2.13
```

With this branch: **239 passed**, the other two legs unchanged.

Every prior audit went at the 402 **before** the money moves — hostile amounts, hostile
containers, the payee pin. This run went at the one moment that is strictly worse than any
of them: **the seller has taken the XNO and then does not serve the call.**

## Checked

- **The payee pin, both halves.** `_call_vend` refuses a `payTo` that is not
  `VEND_TREASURY_DEFAULT` before a wallet is handed over (`tools.py:289`), and
  `_PayeePinnedWallet.build_payment_block` (`tools.py:242`) refuses again at the only
  moment that is the real one — the block being built — because
  `request_with_payment` makes its own 402 read and signs whatever *that* read names.
  Unchanged by this branch.
- **No float touches an amount.** `raw_to_xno` is integer `divmod`; the one decimal
  conversion runs in a 60-digit `localcontext` with `Inexact` and `Overflow` trapped and
  refuses rather than rounds (`tools.py:99-124`). Unchanged.
- **The cap is the amount quoted**, integer raw, nothing wider: `int(quote["amount_raw"])`
  (`tools.py:452`). A seller that raises the price on the paying call is refused by
  `request_with_payment`, not paid. Unchanged.
- **Zero and unpriced quotes.** `not quote.get("amount_raw") or int(...) <= 0` refuses
  before the wallet is reached (`tools.py:278`), so `"0"` — a non-empty string — cannot
  become `max_raw=0`. Unchanged.
- **`httpx.get` does not follow redirects** (httpx defaults to `follow_redirects=False`),
  so the dry-run read cannot be walked to another host; a 3xx comes back as a non-200 and
  is refused. Re-checked against the installed httpx this run.
- **The declared dependency signature still matches the installed library**: one signing
  call (`wallet.build_payment_block`) in `nano_pay/x402.py` 0.2.13, no second signing path
  for the wrapper to miss.

## Found and fixed — a paid call the seller refused threw the receipt away

`request_with_payment` returns `(response, receipt)`, and **the payment has already
settled by the time it returns**. So a non-200 in `paid` is not a failed call: it is a
*paid* call that was not answered. `tools.py:459` on `main`:

```python
paid, receipt = request_with_payment(...)
if paid.status_code != 200:
    raise RuntimeError(
        f"Vend {url} -> HTTP {paid.status_code} after payment: {paid.text[:200]}"
    )
return {"result": paid.json(), "payment": receipt or {}}
```

`receipt` is bound on the line above and then dropped. What the agent received, measured
on `d3f1d4e` with the seller answering 502 after settling:

```
RuntimeError: Vend https://search.paypercall.dev/api/v1/web-search -> HTTP 502
              after payment: upstream unavailable
```

A signed block was handed to the seller, and the block hash and the library's verdict on
it are not in that sentence, not on the exception, and not anywhere else the caller can
reach — `_run` re-raises everything but `Vend402Quote`, so nothing downstream sees the
receipt either. Whether the XNO actually moved is not something the status code says: in
feeless402 0.2.13 the receipt's `settled` is True (the block is on the ledger: the XNO was
spent), False (the seller refused and the ledger does not hold the block: nothing was paid)
or `"indeterminate"` (neither confirmed it), with `ledger` and, when not True, a `note`.
Things an agent can do with that receipt, all of them impossible without it:

1. quote the block to the seller, who can look it up on the public ledger;
2. check the block on the ledger itself when `settled` is not True;
3. tell this apart from a call it never paid for.

What it cannot do is retry for free: this tool's input is only the query, it has no way to
re-present a block in `X-PAYMENT`, and calling it again can sign a new payment.

A 502 from a proxy in front of a pay-per-call endpoint is an ordinary event, and the module
already treats one as ordinary on the *unpaid* read (`challenge = {}` for a non-JSON 402
body, `tools.py:265`). On the paid read it was the one failure that costs money and the one
with the least information.

The fix raises `VendPaidButNotServed` instead, carrying `receipt`, `block`, `amount_raw`,
`settled`, `ledger`, `note`, `url`, `status_code` and `body`, and naming the block, the
amount and the receipt's own verdict (and note) in the message text as well — a LangChain
agent reads the message far more often than it introspects the exception. The message says
the XNO was spent only when `settled` is True, says nothing was paid when it is False, and
says the outcome is unknown otherwise; it warns that calling the tool again can make a new
payment and never suggests putting the block in `X-PAYMENT`. It keeps the old
`... after payment: ` prefix and ends with the seller's body (cut to 200 characters), and it
round-trips through `pickle`, `copy.copy` and `copy.deepcopy`. It subclasses `RuntimeError`, so every handler that caught the old error still
catches this one, and it is exported from the package.

Nothing about a served 200 changes, and nothing about the refusals before the money moves
changes: this is strictly about what survives a failure after it.

```
$ pytest -q tests/test_live_payment.py -k "paid_call or block_hash or runtime_error or no_block or no_receipt or served_200"
  4 failed, 2 passed        # without the fix
$ pytest -q
  252 passed                # with it (was 233)
```

The two that pass either way are deliberate: `..._still_a_runtime_error_so_existing_handlers_keep_working`
pins the compatibility, and `..._a_served_200_is_untouched_by_any_of_this` pins that the
success path did not move.

## Could not verify

- **Nothing was paid.** No wallet and no network exist in this session; the payment entry
  point is a fake mirroring the installed signature exactly, which
  `.github/dependency_signature_check.py` re-pins against feeless402 0.2.13 on every run.
  Whether a real Vend deployment ever answers a non-200 *after* settling is not something
  this session can observe — the handling is what is fixed, not the frequency.
- **Only `extract.paypercall.dev/.well-known/x402` is reachable from here**, as on 10-12.
  The three endpoint URLs and their `0.0001` prices in `VEND_ENDPOINTS` are unchanged since
  then and were not re-read against the live sellers.
- **`wallet` is a pydantic field on `_VendTool`**, so a `repr()` of the tool, or a tracer
  that serialises its fields, renders the wallet object. Whether that discloses key
  material depends on `nano_pay.wallet.Wallet.__repr__`, which this run read as the pydantic
  default (object identity, no attribute values) — no leak found, and nothing changed.
  Worth re-reading on a feeless402 upgrade, since it is their class and not ours.

# langchain-vend — audit 2026-10-08

Previous audit 2026-10-05. One question: can an agent pay Vend in XNO with this,
today, without being hurt? Audited at `f64ae9b` (`main` after #6). Python 3.11.

## Checked

- `pip install -e ".[test]"`, `python -m compileall -q langchain_vend tests examples`,
  `pytest -q`: **24 passed** on `main`, **31** after this branch. Installed
  *without* the `live` extra, as CI does, so the quote path is exercised with no
  `nano_pay` present.
- `_parse_quote` end to end: the payable-offer filter (`exact` /
  `nano:mainnet` / `XNO` only), `amount` read as integer raw, `_pay_to_of`'s
  list form, and the `payable` flag. The USDC-on-another-network case is refused
  rather than handed an XNO amount.
- `raw_to_xno`: integer `divmod`, no float on any amount. `RAW_PER_XNO = 10**30`.
- `_call_vend`'s refusals, in the order they fire: no payable XNO offer, then
  the treasury pin, then the payment. Dry-run and wallet-less both spend nothing.
- **The payment path through the dependency, not up to it**: `nano_pay/x402.py`
  of `feeless402` 0.2.13 — `request_with_payment`, `pick_nano_offer`,
  `offer_amount_raw`, `offer_pay_to`, `build_payment_header`, the journal
  re-present path and `_settle_outcome`. That read is where this run's finding
  came from.
- Secrets: none in the tree. No seed, no key; the wallet is the caller's object
  and is never serialised or logged.

## Found and fixed (this branch)

**The treasury pin was checked against this package's own read of the 402, and
never against the account the block pays.**

`_call_vend` (`langchain_vend/tools.py:243`) refuses a quote whose `payTo` is not
`VEND_TREASURY_DEFAULT`. It then hands the wallet to
`nano_pay.x402.request_with_payment`, which **makes its own HTTP request** and
signs the payee of *that* read (`nano_pay/x402.py:437-472`):

```python
r = requests.request(method, url, headers=headers, timeout=60, **req_kwargs)
quote  = parse_quote(r)
offer  = pick_nano_offer(quote)
amount = offer_amount_raw(offer)
pay_to = offer_pay_to(offer)
if amount > max_raw:                                   # the amount is checked
    raise PriceCapExceeded(...)
block, ... = wallet.build_payment_block(rpc, pay_to, amount)   # pay_to is not
```

There is no destination parameter, so an endpoint that quotes the treasury on the
read `_call_vend` checks and names another account on the read that is paid gets
the block signed to that other account. Measured on the shipped code, with the
two reads differing only in `payTo`:

```
checked payTo (first read) : nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7
pinned treasury            : nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7
BLOCK SIGNED TO            : nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3
                             100000000000000000000000000 raw (0.0001 XNO)
receipt pay_to             : nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3
result                     : served, settled: True
```

The amount is correct, the cap is satisfied, the call is served, and the money is
in an account this package is not pinned to — while `README.md` says every
payment settles to the published swarm treasury.

**Why no test could see it.** Every fake in `tests/test_live_payment.py` replaces
`request_with_payment` wholesale and re-reads nothing, so the pin was only ever
tested against the one read it already governs. The new fixture mirrors
feeless402's real body instead: it re-reads the 402 through `httpx.get` and calls
`wallet.build_payment_block(rpc, pay_to, amount)` with the payee of that read.

**The fix** is `_PayeePinnedWallet`, a proxy applied on every live payment:
`build_payment_block` refuses when `to_addr` is not the account already checked,
so no block exists. `build_payment_block` is the only place the wallet signs, and
everything else (`path` for the journal, `account()` for the replay proof,
`payment_succeeded` / `payment_failed`) forwards to the real wallet.

This **only adds a refusal**: no amount, rounding, key path or destination
changes, and the single account allowed is the one `_call_vend` had already
checked against `VEND_TREASURY_DEFAULT`. `VEND_PAY_TO` still moves the pin.

**The pin's own assumptions are now a CI law.** The proxy intercepts exactly one
method and forwards the rest through `__getattr__`, so a dependency that signed
through a different call, or took the destination in a different position, would
have the real signing call forwarded untouched — a refusal that stopped refusing,
with a green suite, because the offline fake calls whatever the proxy declares.
`.github/dependency_signature_check.py` now asserts both against the real
feeless402: that `request_with_payment` signs through `wallet.build_payment_block(`
and that `Wallet.build_payment_block`'s first three parameters are
`(rpc, to_addr, raw_amt)`. Both were mutation-checked: a renamed signing call and
a swapped argument order each make the check exit 1 with the reason named.

Failing-then-passing, `langchain_vend/tools.py` alone reverted to `main` with the
tests kept: **3 failed, 28 passed**. Five of the eight additions are controls
that hold either way — an unchanged destination is still paid, a payee restated
identically on the paying read is still paid, `VEND_PAY_TO` still moves the pin
at signing time, the proxy forwards the rest of the wallet, and a price raised on
the paying read is still refused by `max_raw`.

## Found, not fixed here

**`_parse_quote`'s `price_xno` fallback still rounds at 28 digits — PR #5, open
since 2026-10-04 and the owner's by the merge rule** (it converts exactly, which
moves an amount, so it is not refusal-only). One thing worth adding to it that
this run established and the PR does not say: the defect is **first-call-only in
a long-lived process**. `nanopy/__init__.py:19` runs
`decimal.getcontext().prec = 40`, and `nano_pay` is imported lazily inside
`_call_vend` — so the first quote of a process parses at the default precision of
28 and every later one at 40. An intermittent wrong amount is harder to diagnose
than a consistent one, which strengthens the case for the `localcontext` the PR
already uses rather than leaning on a dependency's import side effect.

## Could not verify

- **No live payment.** Every figure here is measured against stand-ins for
  `httpx` and `nano_pay`; no wallet, no node and no network were reached, and no
  XNO moved. `test_live_dryrun.py` needs the real endpoints, which this
  environment's egress policy does not reach.
- **The journal re-present path against the pin.** `_journal_key` includes
  `pay_to`, and a journal hit returns a stored header without calling
  `build_payment_block`, so the pin is not consulted there. That path
  re-presents a block signed in an earlier run and moves no new money, which is
  the correct behaviour — but an entry signed to a moved destination *before*
  this fix would still be re-presentable. No such entry is known to exist.
- **Whether any real Vend deployment has ever answered two differing reads.**
  The exposure is a property of the code, not an observed event.

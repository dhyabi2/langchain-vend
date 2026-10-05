# langchain-vend — audit 2026-10-05

Lens: can an agent pay Vend in XNO with this today, without being hurt. Audited
at `ee5575f` (`main` after #4).

## Checked

- `langchain_vend/tools.py` end to end: `_payable`, `_pay_to_of`, `_parse_quote`,
  `_VendTool._call_vend`, `_run`/`_arun`, `_quote_json`, the three tool classes.
- `langchain_vend/__init__.py`, `README.md`, `llms.txt`, `examples/`,
  `test_live_dryrun.py`, `pyproject.toml`, `.github/workflows/test.yml`.
- Amount handling: `raw_to_xno` (integer `divmod`, no float anywhere) and both
  branches of `_parse_quote` against the recorded live 402
  (`price_xno: 0.0001`, `accepts[0].amount: "100000000000000000000000000"`).
- The live payment call against the signature `feeless402` really declares.
  Installed 0.2.13 in this session and read `nano_pay/x402.py`:
  `request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
  dry_run=False, prework=False, **req_kwargs)`. `.github/dependency_signature_check.py`
  passes against it.
- Secret scan of the tree. None found: the only account in the tree is the
  published treasury (a public receiving address), there is no seed, key or
  token, and no `.env`.
- `python3 -m pytest -q`: **18 passed** on `main`, **24** after this change.
  `compileall` clean.

## Found and fixed (this PR)

**`max_raw` capped the amount. Nothing capped the destination.**

`_call_vend` (`tools.py:185-192`) hands the payment to
`request_with_payment(..., max_raw=int(quote["amount_raw"]), ...)`. That
signature has no destination parameter, so the account the money lands in is
whichever `payTo` the 402 named. Meanwhile:

- `tools.py:41` says "All Vend payments settle to the swarm treasury - single
  destination, no issuer";
- `tools.py:42-45` exports `VEND_TREASURY_DEFAULT`, env-overridable through
  `VEND_PAY_TO`, and `__init__.py:8` re-exports it;
- `README.md` says "All payments settle to the published swarm treasury account."

**No line of code read that constant.** `grep` for it finds the definition and
the two re-exports and nothing else. So a 402 that answered with any other
`payTo` was paid the quoted price, to that account, and the quote the agent read
before deciding showed the foreign address under a README promising a fixed
destination. A price cap is not a destination pin.

The fix refuses it, in `_call_vend`, **before** `nano_pay` is imported and before
the wallet is handed over:

```
the 402 quotes a destination this package is not pinned to; nothing was paid.
expected "nano_1yo6…jmnx7", quoted "nano_3t6k…uohr3"
(set VEND_PAY_TO to pay a different Vend deployment)
```

`VEND_PAY_TO` moves the pin to another account rather than removing it, which is
what a second Vend deployment needs and what a hostile 402 cannot do.

Failing-then-passing, `langchain_vend/tools.py` alone reverted to `main` with the
new tests kept: **4 failed, 20 passed** —
`..._destination_we_are_not_pinned_to_is_refused_not_paid`,
`..._names_both_accounts_so_the_agent_can_tell_them_apart`,
`..._payable_amount_but_no_destination_is_refused`,
`..._vend_pay_to_moves_the_pin_rather_than_removing_it`. Three of the nine
additions are controls that hold either way, so the refusal cannot quietly cost
a good payment: the recorded live treasury quote is still paid, and dry-run
quoting still reports a foreign destination instead of refusing, because reading
a price spends nothing and seeing the odd account is how an agent finds out.

## Found, not fixed here (one concern per branch)

- **`price_xno: 0` yields a "payable" quote of 0 raw.** `tools.py:131` admits
  `xno >= 0`, while the raw-amount branch at `:117`/`:120` requires `> 0`. A
  payable Nano offer with no usable `amount` and `price_xno: 0` therefore
  produces `payable_in_xno: true, amount_raw: "0"` and the note "Send exactly
  amount_raw ... to pay_to" — a call that is not free, quoted as free, and a
  live attempt then passes `max_raw=0` and fails inside the dependency with
  nothing saying why. One-line tightening (`xno > 0`), left for its own branch.

## Could not verify

- The live endpoints. `https://search.paypercall.dev/api/v1/web-search` returns
  **403 from this session's egress proxy**, not from Vend, so the current 402 was
  not captured first-hand. The pinned account is the one the recorded 402 in
  `tests/test_quote.py` and `tests/test_live_payment.py` names, and the fix is
  tested against it, so the pin matches the destination the repository's own
  recorded live answer quotes. **If Vend has since rotated its treasury, the pin
  must move with it** — that is a `VEND_PAY_TO`/constant update, and the refusal
  names both accounts so the mismatch is unambiguous rather than silent.
- `payTo` as a **list**. `_pay_to_of` takes the first entry; the pin compares that
  one. Which entry `nano_pay` pays when a list is quoted was not established, so
  a list naming the treasury first and another account second is pinned on the
  first element only. No observed Vend 402 sends a list.
- A real wallet, a real RPC and a real broadcast: none of them exist in this
  session, and the suite opens no socket by design.

# langchain-vend — code audit, 2026-10-10

Previous audit 2026-10-09 (`e8e6012`, an unreadable amount refused rather than crashed on).
HEAD audited: `main` at `3ab9df4` ("The quoted price was rounded to 28 digits: a spend cap one raw
short of the quote (#5)"). Read under one question: can an agent pay in XNO with this today
without being hurt?

Baseline on `main`, the three legs CI runs, before any change:

```
$ python -m compileall -q langchain_vend tests examples      # clean
$ pip install -e ".[test]" && pytest -q                      # 54 passed
$ pip install -e ".[test,live]"
$ python .github/dependency_signature_check.py               # OK, feeless402 0.2.13
```

With this branch: **56 passed**, the other two legs unchanged.

This run went after the payee pin and the amount, and checked both against the real
`feeless402 0.2.13` source rather than against the fake the offline suite uses.

## Checked

- **The payee pin holds at the only moment that counts.** `_PayeePinnedWallet`
  (`tools.py:215-245`) wraps the wallet and refuses `build_payment_block` to any account but the
  one `_call_vend` checked. Read against the installed library: `nano_pay/x402.py:471` is the
  **only** call that signs — `request_with_payment` either re-presents a journalled header
  (`x402.py:467-469`, keyed on method, url, `pay_to`, amount and a request fingerprint, so a
  replay is for the same destination) or calls `wallet.build_payment_block`. There is no second
  signing path for the wrapper to miss in 0.2.13. `Wallet.send` exists but this package never
  reaches it.
- **The three endpoints and their prices, against the seller's own published terms.**
  `extract.paypercall.dev/.well-known/x402` lists exactly
  `https://extract.paypercall.dev/api/v1/extract`,
  `https://search.paypercall.dev/api/v1/web-search` and
  `https://geoip.paypercall.dev/api/v1/geoip`, each at `amount:
  "100000000000000000000000000"` (10**26 raw = 0.0001 XNO) to
  `nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7`. All three match
  `VEND_ENDPOINTS` host for host and path for path, every price matches the `"0.0001"` beside it,
  and the payee matches `VEND_TREASURY_DEFAULT`. Every payable resource in the manifest settles to
  that one account.
- **The README quickstart, call by call.** `Wallet()`, `.exists()`, `.load()`, `.create(seed=None)`
  all exist with those signatures in `nano_pay/wallet.py`; `dry_run=False` is what spends, and the
  wheel job already pins `dry_run is True` as shipped. The quoted sample output matches what
  `_quote_json` actually emits (`price_xno` a string, `amount_raw` integer raw).
- **No float ever touches an amount.** `raw_to_xno` is integer `divmod`; the one decimal
  conversion runs in a 60-digit `localcontext` with `Inexact` trapped and is refused rather than
  rounded. 16 hostile challenge shapes were put through `_parse_quote`: a float amount, a negative
  integer amount, a 40-digit amount, a `payTo` list, a missing `payTo`, a USDC-first `accepts`, a
  USDC-only body carrying `price_xno`, and a negative `price_xno` all come out either exact or
  unpriced.
- **Branches and pull requests.** Eight remote branches, eight pull requests, all merged. **0 open
  pull requests, 0 orphans.**

## Found and fixed — a quote of zero raw reached the spend path

`_call_vend` guarded the paying call with `if not quote.get("amount_raw")` (`tools.py:271` on
`main`). `amount_raw` is a **string**, and `"0"` is a non-empty string, so the guard was False for
it and a quote of zero went on to `request_with_payment(..., max_raw=0)`. Measured on `3ab9df4`
with a 402 whose Nano offer carries no amount and whose body says `price_xno: 0` (the one shape
that yields `amount_raw == "0"` with `payable` True):

```
REACHED SPEND PATH: request_with_payment reached with max_raw=0
quote was: {'price_xno': '0', 'amount_raw': '0', ..., 'payable': True}
```

Zero is not a payable amount. Downstream, `nano_pay` then either refuses with `quote ... exceeds
cap 0 XNO` — a cap the agent never set, reported as if it had — or, when the paying read also says
zero, calls `wallet.build_payment_block(rpc, pay_to, 0)` and has the wallet **sign a block that
moves nothing**: proof of work spent and a journal entry written for a payment that paid nobody.

Fixed by refusing a non-positive amount with the message a 402 that priced nothing already gets.
This only narrows what is accepted: no amount, destination, rounding or key path changes, and the
dry-run read is untouched — the quote still reports `"0"` to the agent. One test fails on
`3ab9df4` and passes here.

## Found, not fixed — needs the owner's call (a tested decision, not a slip)

**A Nano offer whose amount says zero or negative is priced from `price_xno` instead.**
`_parse_quote` (`tools.py:141-153`) treats `amount` text that `RAW_AMOUNT` rejects, or that is
`"0"`, as "no usable raw amount" and falls through to the body's top-level `price_xno` field.
Measured on `3ab9df4`:

| `accepts[0].amount` | body `price_xno` | quote |
| --- | --- | --- |
| `"0"` | `5` | **payable, `amount_raw` 5·10³⁰** — a cap 50 000× Vend's price |
| `"-5"` | `0.0001` | payable, `amount_raw` 10²⁶ |
| absent | `"133248297"` | payable, `amount_raw` 1.33·10³⁸ |

`price_xno` is Vend's own convenience field and sits **outside** the x402 `accepts` structure, so
in these rows the spend cap comes from somewhere the offer is not. An offer that names `"0"` has
named a price — zero — and arguably must not be re-priced from anywhere.

This is **deliberate and asserted**:
`tests/test_quote.py::test_a_numeric_amount_in_another_notation_is_still_rescued_by_price_xno`
lists `"0"` and `"-5"` among the amounts it requires to stay payable, with the rationale that "the
seller did name a price". That rationale fits `1e26` and `0.0001`; it does not fit `"0"` or
`"-5"`. Flipping it reverses an earlier decision on the spend path, so it is not a routine's to
change: the pinned payee means the money can still only reach the treasury, which bounds the harm
to an overpayment to a destination the operator chose.

## Could not verify

- None of the three endpoints is reachable from this session — the agent proxy answers `403` for
  `search.`, `geoip.` and `check.paypercall.dev` before the request leaves (`extract.paypercall.dev`
  is allowed, which is how the published terms above were read). The live 402 bodies were therefore
  read from the seller's `.well-known/x402` manifest and from the fixture in `tests/test_quote.py`,
  not by calling the endpoints. `test_live_dryrun.py` remains the check a person runs.
- No payment was made and no wallet exists in this session, so the signing path was verified by
  reading `feeless402 0.2.13` and by the dependency signature check, not by spending.

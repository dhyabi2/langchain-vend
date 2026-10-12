# langchain-vend — code audit, 2026-10-12

Previous audit 2026-10-10 (`3ab9df4`, the quoted price rounded at 28 digits). HEAD audited:
`main` at `42cbcb5` ("A quote of zero raw reached the spend path (#9)"). Read under one
question: can an agent pay in XNO with this today without being hurt?

Baseline on `main`, the three legs CI runs, before any change:

```
$ python -m compileall -q langchain_vend tests examples      # clean
$ pip install -e ".[test]" && pytest -q                      # 56 passed
$ pip install feeless402 && python .github/dependency_signature_check.py
  OK: langchain_vend's live payment call binds to feeless402 0.2.13
```

With this branch: **66 passed**, the other two legs unchanged.

This run went at the one input the package cannot choose: the shape of the seller's 402
body. Every prior audit put hostile *amounts* through `_parse_quote`; none had put a
hostile *container* through it.

## Checked

- **The payee pin, re-read against the installed library.** `_PayeePinnedWallet`
  (`tools.py:215-245`) still wraps the wallet, and `nano_pay/x402.py` in feeless402
  0.2.13 still has exactly one signing call (`wallet.build_payment_block`) plus the
  journalled-header replay, which is keyed on `pay_to`. No second signing path for the
  wrapper to miss. Unchanged by this branch.
- **No float touches an amount.** `raw_to_xno` is integer `divmod`; the one decimal
  conversion runs in a 60-digit `localcontext` with `Inexact` trapped and refuses rather
  than rounds. Unchanged by this branch.
- **The amount and destination guards in `_call_vend`** (`tools.py:277-297`): a
  non-positive amount and a `payTo` that is not `VEND_TREASURY_DEFAULT` are both refused
  before a wallet is handed over. Unchanged by this branch; the fix below reaches the
  first of them in cases that used to raise instead.
- **The three endpoints and their prices** against `VEND_ENDPOINTS`: unchanged since
  10-10, and `extract.paypercall.dev/.well-known/x402` is the only one this session can
  reach (see *Could not verify*).
- **Branches and pull requests.** Nine remote branches, nine pull requests, all merged.
  **0 open pull requests, 0 orphans** before this branch.

## Found and fixed — a malformed 402 body crashed the price read

`_parse_quote` read `challenge.get("accepts")` and then `accepts[0]`, and neither read
checked a type. x402 says `accepts` is an array of offers; a seller is free to send
something else, and what came back decided whether an agent could ask the price at all.
Measured on `42cbcb5`:

| 402 body | on `main` |
| --- | --- |
| `"accepts": {"0": {…}}` (a map keyed by index) | **`KeyError: 0`** |
| `"accepts": {"offer": {…}}` | **`KeyError: 0`** |
| `"accepts": 1` | **`TypeError: 'int' object is not iterable`** |
| the whole body a JSON array | **`AttributeError: 'list' object has no attribute 'get'`** |
| the body not JSON at all (a proxy's HTML 502 page) | **`json.JSONDecodeError`** from `resp.json()` |

A map keyed by index is not an exotic shape: it is what several JSON encoders emit for a
list, so an ordinary seller bug produces it. Each row raised out of the **dry-run** read,
where the whole point is to learn the price while spending nothing — the same failure
`#8` fixed for an amount that is not an amount, one level up, in the container.

Fixed by refusing a `challenge` that is not an object and an `accepts` that is not a
list or tuple, and by treating a 402 body that will not parse as JSON as having priced
nothing. All five rows now yield an unpriced quote: `payable` False, the agent told to
send nothing, and the paying call refusing with `the 402 carries no payable XNO offer`.

This **only narrows** what is accepted: no amount, destination, rounding or key path
changes, and a well-formed 402 is untouched (`test_a_well_formed_402_is_untouched_by_the_type_checks`
pins the live Vend body and a tuple-valued `accepts` as still payable). **8 tests fail on
`42cbcb5` and pass here**; 56 → 66.

## Found, not fixed — still the owner's call, unchanged from 10-10

**A Nano offer whose amount says `"0"` or `"-5"` is re-priced from the body's
`price_xno`** (`tools.py:141-153`), so the spend cap comes from a field outside the
`accepts` structure. Asserted deliberately by
`test_a_numeric_amount_in_another_notation_is_still_rescued_by_price_xno`; flipping it
reverses an earlier decision on the spend path. The pinned payee bounds the harm to an
overpayment to the operator's own treasury. Reported 10-10, repeated here unchanged.

## Could not verify

- `search.paypercall.dev` and `geoip.paypercall.dev` are answered `403` by the agent
  proxy before the request leaves, so no live 402 was read this run; the fixture in
  `tests/test_quote.py` and the `extract.` manifest stand in for them.
  `test_live_dryrun.py` remains the check a person runs.
- No wallet exists in this session and nothing was paid, so the signing path was verified
  by reading `feeless402 0.2.13` and by the dependency signature check, not by spending.

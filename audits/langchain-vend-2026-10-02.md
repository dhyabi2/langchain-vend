# langchain-vend — audit 2026-10-02

Scope: can an agent pay Vend in XNO with this today, without being hurt.
Read end to end: `langchain_vend/tools.py`, `README.md`, `tests/test_quote.py`,
`examples/`, `pyproject.toml`. Suite before: 6 passed. After: 12 passed.

## Checked

- `_parse_quote` against five 402 shapes: the recorded live Vend 402, the same body with
  `price_xno` removed (every non-Vend x402 seller), a USDC-only 402 with and without a
  `price_xno` field, and a body carrying `price_xno` with no `accepts` at all.
- The dry-run string an agent actually reads (`_VendTool._quote_json`), not just the dict.
- Both documented usages, exercised with `httpx.get` stubbed to return a 402: `VendWebSearch()`
  and the README's live `VendExtract(wallet=w)`.
- The three endpoint URLs, live. `extract.paypercall.dev/api/v1/extract` answers **200**, not
  402: it is serving a 5-call-per-IP free trial (`"free_trial": true, "trial_limit": 5`). Server
  behaviour, not this repository's, and it does not hurt a payer.

## Found and fixed (this PR)

**1. A `price_xno` field made a non-Nano 402 look payable in XNO, and quoted an XNO amount
against a foreign destination.** `_parse_quote` (`tools.py:118`) ran its `price_xno` fallback
whenever the primary path produced no amount — including when there was no payable Nano offer at
all. `_payable()` had been added to stop exactly this, and
`test_a_402_with_no_payable_nano_offer_yields_no_price` passed only because its fixture had no
`price_xno` field. Add one and the guard collapsed:

```
{"price_xno": 0.0001,
 "accepts": [{"scheme":"exact","network":"base","asset":"USDC",
              "amount":"1000","payTo":"0x…dead"}]}
```
quoted, verbatim, to an agent:
```
{"requires_payment": true, "price_xno": "0.0001",
 "amount_raw": "100000000000000000000000000", "network": "base", "pay_to": "0x…dead",
 "note": "Send exactly amount_raw … to pay_to, then retry …"}
```
The amount came from the XNO convenience field, the destination from the USDC offer, and the note
told the agent to send one to the other. With no `accepts` at all it produced an amount and
`pay_to: null` — "send 0.0001 XNO to nowhere".

Fixed: the fallback now requires a payable Nano offer (`offer is not None`), so `amount_raw` and
`pay_to` always come from the same `exact`/`nano:mainnet`/`XNO` offer. The quote carries an
explicit `payable` / `payable_in_xno` flag plus `asset` and `scheme`, and when nothing is payable
the note says *send nothing* instead of *send exactly*. The fallback's real job — a Nano offer
whose `amount` is not an integer string — still works and is now covered.

**2. The README's "Live (pays on the call)" example does not pay.** `dry_run: bool = True`
(`tools.py:168`) and the gate `if not self.wallet or self.dry_run` (`tools.py:176`) mean
`VendExtract(wallet=w)` returns a quote and spends nothing. Shown: with `httpx.get` stubbed, the
documented live call returns `{"requires_payment": true, …}` and no payment is attempted. An agent
following the README believes it paid and got an answer; it got neither. `dry_run=False` was
documented nowhere.

Fixed in the docs, not the default: the README and the module docstring now show
`dry_run=False` and say a wallet alone leaves the tool in dry-run. Flipping the default would make
the library spend money where it previously did not — that stays the owner's call (carried over
from the 2026-09-30 audit).

Tests: 6 new tests in `tests/test_quote.py`. All 6 fail on the previous code, all 12 pass with the
fix.

## Found, NOT fixed — needs the owner

1. **`dry_run`'s default is still the opposite of what a wallet implies.** Documented now, but
   passing a wallet and getting a quote remains a surprising API. Owner's call.
2. **`VEND_TREASURY_DEFAULT` / `VEND_PAY_TO` is still never used** (`tools.py:38-41`). The live
   path hands the URL and wallet to `nano_pay.x402.request_with_payment`, which re-runs the
   handshake and pays whatever destination and amount the server quotes; the parsed quote is never
   compared against the pinned treasury, and there is no maximum price. Unchanged from
   2026-09-30: pinning the destination and capping the price is a change to the code that sends
   money.

## Could not verify

- No live 402 was captured this run. `extract.paypercall.dev` answers 200 under its free trial,
  and the trial is keyed per IP — this container's egress IP rotates between calls, so the trial
  never exhausts. `search.paypercall.dev` and `geoip.paypercall.dev` are blocked by this
  environment's network policy (proxy `CONNECT` 403), so neither was reached. The fixtures remain
  the recorded 402 from 2026-09-30.
- The live payment branch (`request_with_payment`) is still untested: `feeless402` is not
  installed here, and exercising it would move real XNO.
- `test_live_dryrun.py` was not run (needs network to all three hosts).

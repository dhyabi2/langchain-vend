# langchain-vend — audit 2026-10-02

Scope: can an agent pay Vend in XNO with this today, without being hurt.

Read end to end: `langchain_vend/tools.py`, `langchain_vend/__init__.py`, `README.md`,
`examples/`, `tests/`, `test_live_dryrun.py`, `pyproject.toml`. This run also read the
declared API of the dependency the live path calls (`feeless402` 0.2.11, the `[live]`
extra: `nano_pay/x402.py`, `nano_pay/wallet.py`, `nano_pay/rpc.py`), which is what the
2026-09-30 audit could not do.

## Checked

- `_VendTool._call_vend` for all three tools: 402 -> quote -> pay -> answer.
- The payment call against the real signature of `nano_pay.x402.request_with_payment`.
- `Wallet`'s real constructor against the one the README and module docstring show.
- Amount handling: `_parse_quote` / `raw_to_xno` against the recorded live 402
  (`price_xno: 0.0001`, `accepts[0].amount: "100000000000000000000000000"` raw).
- `python3 -m pytest -q`: 6 passed before this change, 12 after.

## Found and fixed (this PR)

**The live payment path could not run at all — three faults in a row, all of them on
the only code in this package that spends XNO.**

1. `tools.py:170` called

   ```python
   request_with_payment(url, params=params, wallet=self.wallet, timeout=60)
   ```

   `nano_pay.x402.request_with_payment` is declared

   ```python
   request_with_payment(method, url, wallet, rpc, max_raw, headers=None,
                        dry_run=False, prework=False, **req_kwargs)
   ```

   so `url` bound to `method`, and `url`, `rpc` and `max_raw` were missing:
   `TypeError: missing a required argument: 'url'`. No payment was reachable.

2. The function returns `(response, receipt)`. The old code returned that tuple
   straight into `json.dumps` in `_run`, which cannot serialise a response object —
   a second crash behind the first.

3. `dry_run: bool = True` (old `tools.py:67`) meant the README's documented live
   usage, `VendExtract(wallet=w)`, returned a quote and spent nothing, so fault 1
   was never even reached. Reported but not fixed on 2026-09-30, on the grounds that
   flipping the default makes the library spend where it did not. What the dependency's
   signature now shows is that the previous behaviour was not "spends nothing by
   design" but "cannot spend at all": there is no behaviour to preserve. The default is
   now `False`, matching the README; `dry_run=True` with a wallet still only quotes,
   and no wallet still only quotes.

   Fixed: the documented call, `(method, url, wallet, rpc, max_raw, params=...)`, with
   the response body and the settlement receipt returned as
   `{"result": ..., "payment": {...}}`.

**The spend cap `request_with_payment` requires is the exact raw amount the agent was
quoted.** This also closes half of 2026-09-30's second finding ("there is also no
maximum price"): a seller that raises the price between the quote and the paying call
now hits `PriceCapExceeded` and is refused, not paid. A 402 that carries no payable
`exact`/`nano:mainnet`/`XNO` offer raises instead of handing a wallet to the library.

**`Wallet(seed="...")` is not the API.** `nano_pay.wallet.Wallet.__init__` takes a
`path`; the seed lives in that file and arrives via `create(seed=None)` or `load()`.
The README's live snippet and the module docstring both raised
`TypeError: unexpected keyword argument 'seed'`. Both now show `Wallet()` plus
`load()`/`create()`.

Evidence: `tests/test_live_payment.py` (6 tests) replaces `httpx.get` and `nano_pay`
with a fake whose signature mirrors the real one exactly. 4 of the 6 fail on the
previous code (two by `IndexError` — no payment was ever attempted — one on the cap,
one on the unpayable-402 refusal) and all 12 tests in the suite pass with the change.
No network call and no wallet are involved.

## Could not verify

- No XNO moved: this run had no funded wallet. The payment call is proved correct
  against `feeless402`'s declared signature and exercised against a faithful fake, not
  against a live settlement.
- `test_live_dryrun.py` was not run (it needs network). It still carries a stale
  `sys.path.insert(0, "../../territory-dynamo/langchain-vend")` at line 11; harmless,
  since the import resolves from the repo root.
- The README install line (`pip install "langchain-vend[live] @
  git+https://github.com/dhyabi2/langchain-vend.git"`) cannot work for anyone outside
  the account while this repository is private. Not a code fault; the owner's call.

## Still open — the owner's call

`VEND_TREASURY_DEFAULT` / `VEND_PAY_TO` is still never compared against the 402's
`payTo` before paying (2026-09-30, finding 2). The destination is whatever the seller
quotes. Pinning it is a policy decision about where money may go, so it is reported
again rather than made.

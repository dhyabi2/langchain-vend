# langchain-vend — audit 2026-09-30

Scope: can an agent pay Vend in XNO with this today, without being hurt.
Read end to end: `langchain_vend/tools.py`, `langchain_vend/__init__.py`, `README.md`,
`examples/`, `test_live_dryrun.py`, `pyproject.toml`.

## Checked

- The x402 path: `_VendTool._call_vend` (402 -> quote -> optional pay -> retry) for all three
  tools (`VendWebSearch`, `VendExtract`, `VendGeoIP`).
- Amount handling against a recorded real 402 from `search.paypercall.dev/api/v1/web-search`
  (`price_xno: 0.0001` as a JSON float, `accepts[0].amount: "100000000000000000000000000"` raw).
- The three endpoint URLs and the treasury address in `VEND_TREASURY_DEFAULT`.
- Both documented usages: dry-run (`VendWebSearch()`) and live (`VendWebSearch(wallet=w)`).
- Tool construction: each subclass instantiates as a `langchain_core.tools.BaseTool` and resolves
  its own `_endpoint`.

## Found and fixed (this PR)

**The quote misstated the price by a factor of 10\*\*30, and was a float when it did not.**
`_parse_quote` (old `tools.py:50-59`) returned

```python
"price_xno": challenge.get("price_xno") or a0.get("amount"),
```

`accepts[].amount` is integer **raw**, not XNO. Two consequences, both on the number an agent
reads before it decides to pay:

1. Against any standard x402 seller — everyone except Vend, which alone sends the extra
   `price_xno` field — the quote read `price_xno = "100000000000000000000000000"`: 1e26 XNO
   instead of 0.0001 XNO.
2. Against Vend it read the JSON float `0.0001`. A float on an XNO amount cannot be exact at
   10\*\*30 raw to the XNO.

`_parse_quote` also never checked `scheme`/`network`/`asset`, so a seller advertising only USDC on
another network was quoted as an XNO price (`price_xno: "1000"` for a 1000-unit USDC offer).

Fixed: the price is derived from the raw amount of the first offer that is actually
`exact`/`nano:mainnet`/`XNO`, and returned as an exact decimal string alongside the integer
`amount_raw` an agent must send. Vend's `price_xno` is used only as a fallback and is read through
`Decimal(str(...))`, so nothing leaves the function as a float. `tests/test_quote.py` covers it:
6 tests, all 6 fail on the previous code.

The README's sample quote output already documented `"price_xno": "0.0001"` — a string — so the
README was right and the code was wrong. The sample now also shows `amount_raw`.

## Found, NOT fixed — needs the owner

1. **The documented live usage never pays.** `dry_run: bool = True` (`tools.py:67`) and the gate
   `if not self.wallet or self.dry_run` (`tools.py:75`) mean the README's "Live (pays on the call)"
   example, `VendExtract(wallet=w)`, returns a quote and spends nothing. `dry_run=False` is
   required and is documented nowhere. Either the default or the docs are wrong. Not changed here:
   flipping the default makes the library spend money where it previously did not, which is the
   owner's call, not an audit's.

2. **`VEND_TREASURY_DEFAULT` / `VEND_PAY_TO` is never used.** It is defined (`tools.py:38-41`),
   exported, and documented as the single settlement destination — but the payment path never
   compares the 402's `payTo` against it before paying. In live mode `_call_vend` hands the URL
   and wallet to `nano_pay.x402.request_with_payment`, which re-runs the handshake and pays
   whatever destination and amount the server quotes. There is also no maximum price. So the
   module docstring's "Price is known BEFORE any spend" is true only in the sense that the quote
   is parsed — it is never checked against anything. Pinning the destination and capping the price
   is a change to the code that sends money, so it is reported, not made.

## Could not verify

- No live payment was made, and no live endpoint was called: this run had no funded wallet and
  would not spend one. The recorded 402 above is the evidence for the amount handling.
- `test_live_dryrun.py` was not run (it needs network). It also still carries a stale
  `sys.path.insert(0, "../../territory-dynamo/langchain-vend")` at line 11, left from an earlier
  checkout layout; harmless, since the import also resolves from the repo root.
- The live branch (`request_with_payment`) is untested here — `feeless402` is not installed in
  this environment and exercising it would move real XNO.

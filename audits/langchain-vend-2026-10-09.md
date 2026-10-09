# langchain-vend — code audit, 2026-10-09

Previous audit 2026-10-08 (`e8e6012`, which pinned the payee at signing time). Read under one
question: can an agent pay in XNO with this today without being hurt?

HEAD audited: `origin/main` at `e8e6012`. Python 3.11, in a clean virtualenv.

Baseline before any change, the three legs CI runs:

```
$ python -m compileall -q langchain_vend tests examples      # clean
$ pip install -e ".[test]" && pytest -q                      # 31 passed
$ pip install -e ".[test,live]"
$ python .github/dependency_signature_check.py               # OK, feeless402 0.2.13
```

This run followed one question down: every shape a seller's 402 can put in
`accepts[].amount`, and what each one makes the quote say.

## Found and fixed — an amount that is not an amount took the agent down

`_parse_quote` (`langchain_vend/tools.py:120` on `main`) read the seller's amount like this:

```python
if isinstance(amount, str) and amount.strip().isdigit() and int(amount) > 0:
    amount_raw = str(int(amount))
elif isinstance(amount, int) and amount > 0:
    amount_raw = str(amount)
```

`str.isdigit()` and `int()` are not the same test, and `bool` is an `int`. Two consequences,
both measured on the shipped code:

**1. A `ValueError` out of the quote, on the dry-run path.** 128 characters are `isdigit()` but
are refused by `int()` — U+00B2 and U+2074 (superscript two and four), the Ethiopic digits,
U+1369. A 402 carrying `"amount": "²"` raised

```
ValueError: invalid literal for int() with base 10: '²'
```

straight out of `_parse_quote`. `_run` catches only `Vend402Quote`, so it reached the agent as
an exception. This fires in **dry-run**, where the entire point is to learn the price while
spending nothing — so an agent could not even ask what a call costs. A further class passed
`isdigit()` *and* converted: U+0663 (Arabic-Indic three) priced the payment at 3 raw off an
amount written in no ASCII at all.

**2. `"amount": true` was quoted as payable, and the paying call crashed on it.** `bool` is a
subclass of `int`, so `True` took the integer branch and `str(True)` wrote the *text* `"True"`
into `amount_raw`. The quote went out to the agent as

```json
{"payable": true, "amount_raw": "True", "price_xno": "0.000000000000000000000000000001"}
```

and `_call_vend` then does `int(quote["amount_raw"])` to cap the payment. Measured with the
real dependency installed (`feeless402` 0.2.13), against the live code path:

```
ValueError: invalid literal for int() with base 10: 'True'
raised at tools.py:261 ->   int(quote["amount_raw"]),
```

That is **after** the treasury pin at `tools.py:231` has already passed, so the last thing the
agent is told is a `ValueError`, not a refusal and not a payment.

**Fixed** by giving the amount one explicit test — ASCII decimal digits, `^[0-9]+$`, with
`[0-9]` rather than `\d` because `re` matches Unicode digits by default — and by refusing a
`bool` on type before it can reach the integer branch. An amount that names no number in any
notation leaves the offer **unpriced**: `payable` is `False`, `amount_raw` is `None`, and
`_quote_json` tells the agent to send nothing.

**This only ever narrows what can be paid.** The `price_xno` fallback this repository
deliberately keeps — a seller that wrote the raw amount as `1e26` or `0.0001` did name a price,
and `test_price_xno_still_rescues_a_nano_offer_whose_raw_amount_is_unusable` requires it to be
honoured — is untouched: `1e26`, `0.0001`, `-5`, `0`, `+1e26`, `.5` and a missing `amount` all
still resolve to the same `100000000000000000000000000` raw they did before. Only text that is
not a number at all, and `true`/`false`, became refusals. No amount that was already quoted
correctly changes, and nothing that was unpayable became payable.

Fourteen tests added. **Eleven fail with `tools.py` alone reverted to `main`** and pass with the
fix; the other three are controls that must hold either way, so the change cannot quietly cost
a working configuration:

```
test_an_amount_that_is_not_ascii_digits_is_refused_not_crashed_on[²]
test_an_amount_that_is_not_ascii_digits_is_refused_not_crashed_on[⁴]
test_an_amount_that_is_not_ascii_digits_is_refused_not_crashed_on[፩]
test_an_amount_that_is_not_ascii_digits_is_refused_not_crashed_on[٣]
test_an_amount_that_is_not_ascii_digits_is_refused_not_crashed_on[١٢]
test_every_isdigit_character_int_refuses_is_refused_rather_than_raising
test_a_boolean_amount_is_refused_and_never_becomes_the_text_True[True]
test_a_boolean_amount_is_refused_and_never_becomes_the_text_True[False]
test_a_quoted_amount_is_always_an_integer_the_paying_call_can_read
test_an_unreadable_amount_tells_the_agent_to_send_nothing
test_an_unreadable_amount_stops_the_payment_before_a_wallet_is_handed_over
```

`test_every_isdigit_character_int_refuses_is_refused_rather_than_raising` walks the whole
Unicode range rather than trusting the five examples: it asserts the disagreeing set is exactly
128 characters and that every one of them is refused, so a future Unicode table change fails
the test instead of reopening the hole.

After the fix, all three CI legs: **45 passed**, `compileall` clean, and the dependency
signature check still binds to `feeless402` 0.2.13.

## Checked and clean

- **The destination pin holds twice**, which is what the 10-08 audit landed. `_call_vend`
  refuses a `payTo` that is not `VEND_TREASURY_DEFAULT` (`tools.py:231`) *and*
  `_PayeePinnedWallet.build_payment_block` refuses to sign to any other account
  (`tools.py:190`). The second is the one that matters: `request_with_payment` makes its own
  402 read and signs `offer_pay_to()` of *that* read, so a seller that quotes the treasury on
  the checked read and names another account on the paid read meets the refusal at the only
  moment that is real — the block being built. `build_payment_block` is the sole place the
  wallet signs, so a refusal there means no block exists.
- **The spend cap is the quoted amount itself** (`tools.py:261`), so a seller that raises the
  price on the paying call is refused rather than paid.
- **Raw stays an integer end to end.** `raw_to_xno` is integer `divmod` and never a float;
  `_parse_quote` carries `amount_raw` as a decimal string. The one `float()` in the repository
  is `examples/langchain_agent_recipe.py:79`, an example's own assertion about a *quoted*
  price, not a computation of an amount to send — it moves no money and is left alone.
- **Dry-run is the default and paying is opt-in.** `dry_run: bool = True`, and a wallet alone
  still quotes: both a wallet *and* `dry_run=False` are needed to spend.
  `test_readme_live_example_actually_leaves_dry_run` holds the README to that.
- **A non-Nano offer is never handed an XNO amount.** `_payable` requires exactly
  `("exact", "nano:mainnet", "XNO")`, and a USDC-only 402 that happens to carry `price_xno`
  comes back unpriced.
- **No secrets in the tree or in any commit.** Scanned `git log --all -p` for `sk-`, `ghp_`,
  `AKIA`, PEM headers and `seed|private_key|secret|password|token|api_key` followed by a value:
  nothing. The 64-hex strings are all `uv.lock` package digests (2205 `sha256` entries), not
  key material. No seed or wallet file is committed; the wallet stays on the operator's machine.

## Could not verify

- **No live payment, and no XNO moved.** Every figure here is measured against stand-ins for
  `httpx` and the wallet. This environment's network policy reaches none of the README's hosts
  — `https://paypercall.dev`, `https://x402.org` and the live web-search endpoint all return
  no response at all (curl code `000`, a denied connection, **not** a 404), so whether those
  URLs serve what the README claims is not established from here, in either direction.
- **Whether a real seller has ever sent one of these shapes.** The arithmetic and the crash are
  wrong at any such value; how often a 402 carries `"amount": true` is unknowable from here.
  The exposure is conditional on what the endpoint sends, and the endpoint is ours.
- **The `price_xno` 28-digit rounding reported on 2026-10-04** is still open as
  **#5** and is deliberately untouched by this run: it changes an amount on the spend path in
  both directions, so it is not a routine's to merge. It is listed for the owner again.

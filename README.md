# langchain-vend

LangChain tools that call the [Vend](https://paypercall.dev) pay-per-call API and
settle each call in **Nano (XNO)** via [x402](https://x402.org) — the machine-payment
protocol that answers HTTP 402.

No signup, no API keys, no subscriptions. You pay per call, in Nano, only when the
server answers.

## Install

```bash
pip install "langchain-vend[live] @ git+https://github.com/dhyabi2/langchain-vend.git@9e5756b1a01bf39c61b9c7265ca4f98f46c2475c"
```

(pinned to a tested commit: the only tag, v0.1.0, predates the quote and spend-cap fixes #5-#11;
base install is `git+...langchain-vend.git@<commit>`; the `[live]` extra adds `nano_pay` for
the live x402 payment handshake). A PyPI release is tracked separately.

## Tools

| Tool | Endpoint | Price (XNO) |
|---|---|---|
| `VendWebSearch` | search.paypercall.dev/api/v1/web-search | 0.0001 |
| `VendExtract` | extract.paypercall.dev/api/v1/extract | 0.0001 |
| `VendGeoIP` | geoip.paypercall.dev/api/v1/geoip | 0.0001 |

Each is a `langchain_core.tools.BaseTool`, so it works in `create_agent` /
`AgentExecutor` / any LangChain or LangGraph agent, and — through Pydantic AI's
`tool_from_langchain` bridge — in Pydantic AI agents too.

## Usage

Dry-run is safe and is the default: with no wallet the tool returns the live quote
(price, network, pay_to) and spends nothing — an agent can always see the price
before committing.

```python
from langchain_vend import VendWebSearch, VendExtract, VendGeoIP

tool = VendWebSearch()
result = tool.invoke({"q": "nano cryptocurrency"})
# {"requires_payment": true, "payable_in_xno": true, "price_xno": "0.0001",
#  "amount_raw": "100000000000000000000000000", "asset": "XNO",
#  "network": "nano:mainnet", "scheme": "exact", "pay_to": "nano_1yo6..."}
```

`payable_in_xno` is false when the 402 asks for some other rail, or when its
Nano offer names no amount these tools can send. An offer amount must be an
integer number of raw above zero and below 2**128 raw: a JSON integer, or
integer raw text exactly as Python's `int()` reads it (surrounding whitespace,
leading zeros and a single leading `+` allowed); `1e26`, `0.5`, a JSON float, or anything that is not a number leaves
the quote unpriced. The quote then carries no `amount_raw` and no price, and
an agent must send nothing: `asset` and `network` say what the seller asked for.

Live (pays on the call). A wallet alone is not enough: `dry_run` defaults to
`True`, so `VendExtract(wallet=w)` still returns the quote and spends nothing.
Paying is opt-in, and `dry_run=False` is how you opt in. `nano_pay.wallet.Wallet`
takes a file path, not a seed — the seed lives in that file.

```python
from nano_pay.wallet import Wallet
from langchain_vend import VendExtract

w = Wallet()                              # ~/.nano-pay/wallet.json
w.load() if w.exists() else w.create()    # create() takes an optional seed
tool = VendExtract(wallet=w, dry_run=False)   # dry_run=False is what spends
print(tool.invoke({"url": "https://example.com"}))
# {"result": {...the answer...},
#  "payment": {"amount_xno": "0.0001", "block": "...", "settled": true}}
```

The spend cap is the quoted amount itself: a seller that asks for more on the
paying call is refused, not paid. The destination is pinned the same way: a 402
that quotes any account other than the published treasury is refused before the
wallet is handed over, so a cap on the amount is not the only thing standing
between an agent and an account it never meant to pay. Hold a wallet and still
only read the price with `VendExtract(wallet=w, dry_run=True)`.

## How payment works (x402)

1. call the endpoint → HTTP 402 with a payment challenge (price + destination);
2. send that Nano amount to the quoted swarm treasury account;
3. retry with the block hash in `X-PAYMENT` → HTTP 200.

Every call is its own atomic on-chain payment — no running account, no
subscription budget for concurrent requests to race.

## Payments

All payments settle to the published swarm treasury account:

```
nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7
```

That is enforced, not just documented - a live call refuses a 402 quoting any
other `payTo` rather than paying it, and says which account it expected and
which one was quoted. Set `VEND_PAY_TO` to point the tools at a different Vend
deployment; it moves the pin to that account rather than accepting any.

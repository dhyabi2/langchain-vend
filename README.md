# langchain-vend

LangChain tools that call the [Vend](https://paypercall.dev) pay-per-call API and
settle each call in **Nano (XNO)** via [x402](https://x402.org) — the machine-payment
protocol that answers HTTP 402.

No signup, no API keys, no subscriptions. You pay per call, in Nano, only when the
server answers.

## Install

```bash
pip install "langchain-vend[live] @ git+https://github.com/dhyabi2/langchain-vend.git"
```

(base install is `git+...langchain-vend.git`; the `[live]` extra adds `nano_pay` for
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
# {"requires_payment": true, "price_xno": "0.0001",
#  "amount_raw": "100000000000000000000000000",
#  "network": "nano:mainnet", "pay_to": "nano_1yo6..."}
```

Live (pays on the call): pass a wallet. `nano_pay.wallet.Wallet` takes a file
path, not a seed — the seed lives in that file.

```python
from nano_pay.wallet import Wallet
from langchain_vend import VendExtract

w = Wallet()                              # ~/.nano-pay/wallet.json
w.load() if w.exists() else w.create()    # create() takes an optional seed
tool = VendExtract(wallet=w)
print(tool.invoke({"url": "https://example.com"}))
# {"result": {...the answer...},
#  "payment": {"amount_xno": "0.0001", "block": "...", "settled": true}}
```

The spend cap is the quoted amount itself: a seller that asks for more on the
paying call is refused, not paid. Hold a wallet and still only read the price
with `VendExtract(wallet=w, dry_run=True)`.

## How payment works (x402)

1. call the endpoint → HTTP 402 with a payment challenge (price + destination);
2. send that Nano amount to the quoted swarm treasury account;
3. retry with the block hash in `X-PAYMENT` → HTTP 200.

Every call is its own atomic on-chain payment — no running account, no
subscription budget for concurrent requests to race.

## Payments

All payments settle to the published swarm treasury account.

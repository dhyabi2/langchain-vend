# langchain-vend examples

One tool object reaches three agent ecosystems — LangChain/LangGraph, Pydantic AI,
and (via Pydantic AI) any MCP-style client — and pays per call in Nano (XNO) via x402.

- `pydantic_ai_recipe.py` — a Pydantic AI cookbook-style recipe: wrap `VendWebSearch`
  (a langchain_core `BaseTool`) for Pydantic AI with `tool_from_langchain`, prove it
  returns the live 0.0001 XNO quote in dry-run, then batch three tools with
  `LangChainToolset`. Verified against pydantic-ai 2.46.0 + langchain-core 1.6.3.

## Verified output (2026-09-21)

```
Pydantic AI tool name: vend_web_search
Description: Perform a web search and return titles, URLs and snippets. Pay per call in Nano (XNO).
Live dry-run result:
{
  "requires_payment": true,
  "price_xno": 0.0001,
  "network": "nano:mainnet",
  "pay_to": "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7",
  "note": "Send this amount to pay_to, then retry with the block hash in X-PAYMENT."
}
```

No API keys, no subscriptions, no 10% facilitator cut. Price known before any spend.
To pay real calls, pass a `feeless402` `Wallet` to the tool (see `langchain_vend/tools.py`).

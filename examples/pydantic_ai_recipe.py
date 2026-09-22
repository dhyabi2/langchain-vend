"""
Pydantic AI cookbook-style recipe: give a Pydantic AI (or LangChain) Agent a
paid web-search tool that settles per-call in Nano (XNO) via x402.

Reuses the langchain-vend tool (a langchain_core BaseTool subclass). Pydantic AI
exposes `tool_from_langchain` in pydantic_ai.ext.langchain, so ONE tool object
reaches LangChain/LangGraph AND Pydantic AI agents.

This recipe is dry-run: it proves the vendor endpoint emits a real 0.0001 XNO
quote (network nano:mainnet, treasury payTo) and spends nothing. To go live,
build a `Wallet` from feeless402 and pass it to the tool (see langchain_vend/
tools.py), then the agent pays the exact atomic cost per call via x402.

No signup, no API keys, no subscriptions: price known before any spend.

Install:
    pip install langchain-vend pydantic-ai langchain-core

Run:
    python3 pydantic_ai_recipe.py
"""

import json

from langchain_vend import VendExtract, VendGeoIP, VendWebSearch
from pydantic_ai.ext.langchain import tool_from_langchain


def main():
    # --- Step 1: Wrap a langchain-vend BaseTool for Pydantic AI ---
    tool = tool_from_langchain(VendWebSearch())
    print("Pydantic AI tool name:", tool.name)
    print("Description:", tool.description)
    schema = tool.function_schema.json_schema if hasattr(tool, 'function_schema') else "N/A"
    print("Tool schema (JSON):", json.dumps(schema, indent=2))
    # The tool accepts {"q": "your search query"} and returns text.

    # --- Step 2: Dry-run against the live Vend endpoint ---
    # No wallet needed: the tool returns the real Nano 402 quote and spends
    # nothing on the network. This proves the endpoint is live, reachable and
    # answers with the correct price.
    search = VendWebSearch()
    result = search.invoke({"q": "nano cryptocurrency"})
    print("\nLive dry-run result:")
    print(json.dumps(json.loads(result), indent=2))
    # Expected:
    # {
    #   "requires_payment": true,
    #   "price_xno": 0.0001,
    #   "network": "nano:mainnet",
    #   "pay_to": "nano_1yo6c1t...",
    #   "note": "Send this amount to pay_to, then retry with the block hash..."
    # }

    # --- Step 3: Use in a LangChain agent (same tool, different framework) ---
    # The same VendWebSearch() works unchanged in LangChain/LangGraph as a
    # langchain_core.tools.BaseTool — no wrapping needed.
    print("\nLangChain tool name:", search.name)
    print("LangChain description:", search.description)

    # --- Step 4: Multi-framework via LangChainToolset ---
    # Pydantic AI also provides LangChainToolset to batch-wrap a whole set of
    # already-instantiated LangChain tools at once. Building it proves the
    # three tools register cleanly (a Pydantic AI Agent can then be configured
    # with tools=LangChainToolset([...]) at agent construction time).
    from pydantic_ai.ext.langchain import LangChainToolset

    ts = LangChainToolset([VendWebSearch(), VendExtract(), VendGeoIP()])
    print("\nLangChainToolset built ok (id: %s). Wire it to an Agent with "
          "tools=LangChainToolset([VendWebSearch(), VendExtract(), VendGeoIP()])" % ts.id)


if __name__ == "__main__":
    main()

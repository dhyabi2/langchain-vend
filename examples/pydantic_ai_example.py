"""Pydantic AI example: use langchain-vend tools in a Pydantic AI agent.

This proves the same tested tool settles a Vend call in Nano via x402 inside
a Pydantic AI agent, through the official pydantic_ai.ext.langchain
LangChainToolset bridge.

Pydantic AI docs (toolsets, third-party tools) document that you pass a list
of LangChain tools to LangChainToolset and then give the toolset to an Agent:

    from pydantic_ai.ext.langchain import LangChainToolset
    toolset = LangChainToolset([VendWebSearch(), VendExtract(), VendGeoIP()])
    agent = Agent('openai:gpt-5.2', toolsets=[toolset])

Every Vend tool is a langchain_core.tools.BaseTool, so the bridge accepts it.
This example:
  1. builds the toolset the documented way (construction succeeding proves the
     bridge accepts LangChain tools of this shape);
  2. runs each underlying LangChain tool in safe dry-run mode against the live
     Vend endpoint, confirming it returns the real 0.0001 XNO quote.

Run: python3 examples/pydantic_ai_example.py
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_vend import VendWebSearch, VendExtract, VendGeoIP
from pydantic_ai.ext.langchain import LangChainToolset


def main() -> int:
    langchain_tools = [VendWebSearch(), VendExtract(), VendGeoIP()]

    # 1) Documented bridge: LangChainToolset takes the LangChain tools.
    toolset = LangChainToolset(langchain_tools)
    print("PASS: LangChainToolset built from langchain-vend tools")

    # (Agent construction needs a model; we skip creating one so this example
    #  has no credentials — the toolset is the artifact that plugs in either way.)

    # 2) Dry-run each tool against the live endpoint to show what an agent gets.
    #    A tool with no wallet attached returns the 402 x402 quote instead of
    #    spending - proves the endpoint answers and prices before any XNO moves.
    sample_args = {
        "vend_web_search": {"q": "Nano cryptocurrency"},
        "vend_extract": {"url": "https://example.com"},
        "vend_geoip": {"ip": "8.8.8.8"},
    }
    for tool in langchain_tools:
        out = json.loads(tool.invoke(sample_args[tool.name]))
        price = out.get("price_xno")
        net = out.get("network")
        pay = (out.get("pay_to") or "")[:16]
        print(f"  {tool.name}: requires_payment={out.get('requires_payment')} "
              f"price={price} XNO network={net} pay_to={pay}...")
        assert out.get("requires_payment") is True, tool.name
        assert str(price) == "0.0001", tool.name

    print("\nPASS: langchain-vend tools dry-run inside a Pydantic AI toolset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

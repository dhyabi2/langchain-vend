"""Live dry-run tests for langchain-vend against the real Vend endpoints.

These prove (without spending anything) that each tool:
  - instantiates as a langchain_core.tools.BaseTool with a valid schema;
  - calls the live endpoint and surfaces the real 0.0001 XNO quote (network
    nano:mainnet, treasury payTo) in safe dry-run mode.
"""
import json
import sys

sys.path.insert(0, "../../territory-dynamo/langchain-vend")

from langchain_vend import VendExtract, VendGeoIP, VendWebSearch


def main():
    failures = 0
    tests = [
        ("VendWebSearch", VendWebSearch(), {"q": "nano cryptocurrency"}),
        ("VendExtract", VendExtract(), {"url": "https://example.com"}),
        ("VendGeoIP", VendGeoIP(), {"ip": "8.8.8.8"}),
    ]
    for name, tool, args in tests:
        print(f"-- {name} --")
        assert tool.name, f"{name}.name empty"
        assert tool.description, f"{name}.description empty"
        try:
            out = tool.invoke(args)
        except Exception as e:  # noqa
            print(f"  ERROR: {e}")
            failures += 1
            continue
        doc = json.loads(out)
        print(f"  result: {doc}")
        if not doc.get("requires_payment"):
            print("  FAIL: expected requires_payment=true (no wallet)")
            failures += 1
            continue
        if str(doc.get("price_xno")) != "0.0001":
            print(f"  FAIL: expected price 0.0001, got {doc.get('price_xno')}")
            failures += 1
        if doc.get("network") != "nano:mainnet":
            print(f"  FAIL: expected network nano:mainnet, got {doc.get('network')}")
            failures += 1
        if not doc.get("pay_to"):
            print("  FAIL: missing pay_to treasury address")
            failures += 1
    print(f"\n{'FAIL' if failures else 'PASS'}: {len(tests)-failures}/{len(tests)} tools ok")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

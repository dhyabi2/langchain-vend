"""Bind this package's live payment call to the signature feeless402 really declares.

`tests/test_live_payment.py` exercises the payment path against a hand-written
stand-in for `nano_pay.x402.request_with_payment`. That is the right shape for an
offline suite - it must not touch a wallet or the network - but it means the suite
cannot see the one defect that has actually shipped here: the call was written
`request_with_payment(url, params=..., wallet=..., timeout=60)` while the real
entry point is declared `(method, url, wallet, rpc, max_raw, ...)`, so `url` bound
to `method`, three required arguments were missing, and the live path could not pay
at all. A green suite said nothing, because the fake moved with the caller.

This check closes that gap the only way it can be closed: by importing the real
dependency and asking whether the arguments this package passes actually bind to
it. It is deliberately a hard check rather than a best-effort one - `live` is a
declared extra of this project, so if the dependency's entry point moves, this
package's documented live usage is broken and CI should say so.

Nothing here touches the network, a wallet, or a seed: binding is checked with
`inspect.Signature.bind`, which resolves arguments without calling anything.
"""
import inspect
import sys


def _fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def main():
    from nano_pay.x402 import request_with_payment
    from nano_pay.wallet import Wallet
    import nano_pay.rpc as rpc_mod

    sig = inspect.signature(request_with_payment)

    # Exactly the call langchain_vend/tools.py makes, with placeholder values.
    # `params` is the only keyword it passes through to the request.
    try:
        bound = sig.bind("GET", "https://example.invalid/api/v1/web-search",
                         object(), object(), 100000000000000000000000000,
                         params={"q": "x"})
    except TypeError as e:
        _fail("langchain_vend/tools.py calls nano_pay.x402.request_with_payment with "
              f"arguments that do not bind to its real signature {sig}: {e}")

    # The five positional arguments must land where tools.py means them to.
    names = list(bound.arguments)
    for i, expected in enumerate(("method", "url", "wallet", "rpc", "max_raw")):
        if names[i] != expected:
            _fail(f"nano_pay.x402.request_with_payment parameter {i} is {names[i]!r}, "
                  f"not {expected!r}; the positional call in langchain_vend/tools.py "
                  f"would pass that value to the wrong parameter. Real signature: {sig}")

    # tools.py relies on the default: it never passes dry_run, so the dependency's
    # own default decides whether an opted-in call really spends.
    dry_run = sig.parameters.get("dry_run")
    if dry_run is None:
        _fail(f"nano_pay.x402.request_with_payment has no dry_run parameter; "
              f"langchain_vend/tools.py assumes paying is what an opted-in call does. "
              f"Real signature: {sig}")
    if dry_run.default is not False:
        _fail(f"nano_pay.x402.request_with_payment now defaults dry_run="
              f"{dry_run.default!r}. langchain_vend/tools.py does not pass dry_run, so a "
              f"caller that opted in with dry_run=False would silently NOT pay and the "
              f"tool would report a result it never bought.")

    # It returns (response, receipt); tools.py unpacks two values.
    # The docstring/annotation is all that can be read without calling it.
    ret = sig.return_annotation
    if ret is not inspect.Signature.empty:
        print(f"  note: declared return annotation is {ret!r}")

    # README and the module docstring both show Wallet() taking a path, not a seed.
    wsig = inspect.signature(Wallet.__init__)
    if "seed" in wsig.parameters:
        _fail(f"nano_pay.wallet.Wallet.__init__ now accepts a seed ({wsig}); the README "
              f"and module docstring in this package say it takes a file path. One of "
              f"them is wrong and a reader would be handed the wrong snippet.")
    try:
        inspect.signature(Wallet).bind()
    except TypeError as e:
        _fail(f"nano_pay.wallet.Wallet() no longer constructs with no arguments ({wsig}); "
              f"the README snippet `w = Wallet()` is broken: {e}")

    if not hasattr(rpc_mod, "RPC"):
        _fail("nano_pay.rpc.RPC is gone; langchain_vend/tools.py constructs RPC() as the "
              "default Nano RPC for a payment and would raise AttributeError.")

    import nano_pay
    version = getattr(nano_pay, "__version__", "unknown")
    print(f"OK: langchain_vend's live payment call binds to feeless402 {version}")
    print(f"  request_with_payment{sig}")
    print(f"  Wallet.__init__{wsig}")


if __name__ == "__main__":
    main()

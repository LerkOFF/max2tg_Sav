from pathlib import Path


def bootstrap_maxapi() -> Path | None:
    """Compatibility shim for old scripts.

    Max2TG now uses the PyPI package `maxapi-python` (`pymax`) and does not
    require a local MaxAPI checkout.
    """
    return None

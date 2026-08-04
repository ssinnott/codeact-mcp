"""The import surface for helper files.

Helper modules are authored standalone and must be importable on their own — for
editing, for testing, and for the review app's trial runs — so the decorator
lives behind a short, stable name rather than deep inside the server package.

    from codeact import helper
"""

from codeact_mcp.helper import Example, helper

__all__ = ["helper", "Example"]

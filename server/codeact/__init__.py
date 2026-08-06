"""The import surface for helper files.

Helper modules are authored standalone and must be importable on their own — for
editing, for testing, and for the review app's trial runs — so the decorator
lives behind a short, stable name rather than deep inside the server package.

    from codeact import helper
    from codeact.helpers import group_by

The second form is the dependency edge (§12): one name for every edge, so a
helper's reach beyond the standard library is greppable and unambiguous.
"""

from codeact_mcp.helper import Example, helper

__all__ = ["helper", "Example"]

"""Errors shared across domains, kept at the package root to avoid import cycles.

`ToolLookupError` lives here rather than in `aether/tools/` because `aether/tools/__init__.py`
imports the warehouse, so anything the warehouse (or the hotel) needs to import back from the tools
package would form a cycle. A root-level module with no imports of its own cannot.

Why a shared base at all: `ToolRunner` turns "the fixture does not contain that" into a spoken
"I could not find that" rather than a crash. It can only do so if it can *catch* the failure, and
catching one domain's exception class by name would silently break every other domain -- a hotel
lookup for a dish that does not exist would escape as an unhandled error and drop the caller's turn.
One base, caught once, works for every domain that will ever be added.
"""

from __future__ import annotations


class ToolLookupError(KeyError):
    """A tool was asked for something its data does not contain.

    Not an error in the usual sense: it is an answerable outcome. "There is no such dish" is true
    and useful and may be spoken. Distinct from a *stale* result, which may never be spoken at all.
    """

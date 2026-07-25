"""slicegrep — grep that returns ranked, token-budgeted code slices for LLMs.

Public API::

    from slicegrep import focused_read

    result = focused_read("src/", "class Scorer|def score", budget=800)
    print(result.render())        # human/LLM-facing text report
    data = result.to_dict()       # structured data
"""
from .core import (
    Chunk,
    Result,
    SEARCHABLE_EXTS,
    SKIP_DIRS,
    focused_read,
)

__version__ = "0.6.1"

__all__ = [
    "focused_read",
    "Chunk",
    "Result",
    "SKIP_DIRS",
    "SEARCHABLE_EXTS",
    "__version__",
]

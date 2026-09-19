"""包入口。"""

from .models import Category, DiffFile, Finding, Severity, SourceKind
from .diffparse import index_added_lines, parse_unified_diff
from .reviewer import Reviewer, review_diff_text
from .rules import RuleEngine

__version__ = "1.0.0"

__all__ = [
    "Category",
    "Severity",
    "SourceKind",
    "Finding",
    "DiffFile",
    "RuleEngine",
    "Reviewer",
    "review_diff_text",
    "parse_unified_diff",
    "index_added_lines",
    "__version__",
]

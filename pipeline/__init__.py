"""
extractor.pipeline — frozen LangGraph compositions.

Phase 1 ships `graph_v1` plus the version-dispatching `runner.run()` entry
point. Phase 2 adds `graph_v2`, Phase 3 adds `graph_v3`, Phase 4 adds
`graph_v4`. The runner picks the right graph by `--version` flag; old
graphs stay frozen so a Phase 4 fix can't accidentally break Phase 1 demos.
"""

from pipeline.graph_v1 import build_graph_v1, build_graph_v1_dependencies
from pipeline.runner import RunResult, run

__all__ = [
    "build_graph_v1",
    "build_graph_v1_dependencies",
    "run",
    "RunResult",
]

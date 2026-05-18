"""
LangGraph checkpointer factory.

**Phase 1 ships with `MemorySaver`**, LangGraph's in-process checkpointer.
Sufficient because the Phase 1 graph runs to completion in a single process
with no human-in-the-loop interrupts — there is nothing to resume from.

**Phase 4** adds the SME approve/edit/reject loop, which requires the
LangGraph state to survive across process boundaries (the SME may not
respond for hours). That's when `FirestoreCheckpointer` (a real
`BaseCheckpointSaver` implementation backed by the Firestore database
named in `config/storage.yaml`) gets implemented and the factory below
flips to return it.

The factory abstraction lives here so the eventual swap is a one-file
change. Callers (`pipeline/graph_v1.py` → `..._v4.py`) only know about
`build_checkpointer()`.

Environment override:

    CHECKPOINTER_BACKEND=memory      # default in Phase 1
    CHECKPOINTER_BACKEND=firestore   # Phase 4+, raises NotImplementedError today
"""

from __future__ import annotations

import logging
import os
from typing import Any

from core.errors import PipelineError
from core.persistence import StorageConfig

logger = logging.getLogger(__name__)


def build_checkpointer(
    storage_config: StorageConfig | None = None,
    *,
    backend_override: str | None = None,
) -> Any:
    """Return a LangGraph `BaseCheckpointSaver` implementation.

    Resolution order for backend selection:
    1. `backend_override` arg if provided
    2. `CHECKPOINTER_BACKEND` env var
    3. Default = `"memory"` (Phase 1)

    Returns:
        A LangGraph `BaseCheckpointSaver` instance ready to pass into
        `StateGraph.compile(checkpointer=...)`.
    """
    backend = (backend_override or os.getenv("CHECKPOINTER_BACKEND") or "memory").lower()

    if backend == "memory":
        try:
            from langgraph.checkpoint.memory import MemorySaver
        except ImportError as exc:
            raise PipelineError(
                "langgraph not installed — cannot build checkpointer. "
                "Run `pip install -r requirements.txt`.",
                retry_safe=False,
            ) from exc
        logger.info(
            "checkpointer: using MemorySaver (Phase 1 default). "
            "Phase 4 will swap in FirestoreCheckpointer when SME interrupt/resume arrives."
        )
        return MemorySaver()

    if backend == "firestore":
        # Phase 4 deliverable. Sketch of what this will look like:
        #
        #     return FirestoreCheckpointer(
        #         database=storage_config.firestore_database,
        #         collection=storage_config.firestore_checkpoint_collection,
        #     )
        #
        # Implementation needs to subclass langgraph.checkpoint.base.BaseCheckpointSaver
        # and implement get_tuple / list / put / put_writes (sync + async).
        # Each thread_id maps to a Firestore document path:
        #
        #     {collection}/{thread_id}/checkpoints/{checkpoint_id}
        #     {collection}/{thread_id}/writes/{task_id}/{idx}
        raise NotImplementedError(
            "FirestoreCheckpointer is a Phase 4 deliverable (added with the SME "
            "interrupt/resume loop). Phase 1 uses MemorySaver — set "
            "CHECKPOINTER_BACKEND=memory or omit the env var."
        )

    raise PipelineError(
        f"Unknown CHECKPOINTER_BACKEND={backend!r}. Valid: 'memory' | 'firestore'.",
        retry_safe=False,
    )

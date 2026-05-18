"""
extractor.agents — the 3 agent roles every Specialist Team uses.

    Agent              — abstract base; concrete agents subclass it.
    Extractor          — ReAct extractor; Gemini 2.5 Pro + tools + structured output.
    CoverageAuditor    — single LLM call; hunts for misses the Extractor made.
    Arbiter            — gated single LLM call (Flash); resolves conflicts.

Phase 1 instantiates exactly one team (MetadataTeam) using these classes.
Phase 2 instantiates the same classes three more times for the other 3 teams.
"""

from agents.arbiter import Arbiter, ArbiterPolicy, ArbiterResult
from agents.base import Agent, AgentResult
from agents.coverage_auditor import (
    AuditorResult,
    CoverageAuditor,
    MissedField,
    ParserHypothesisMiss,
    SpuriousField,
)
from agents.extractor import Extractor, ExtractorResult

__all__ = [
    "Agent",
    "AgentResult",
    "Extractor",
    "ExtractorResult",
    "CoverageAuditor",
    "AuditorResult",
    "MissedField",
    "SpuriousField",
    "ParserHypothesisMiss",
    "Arbiter",
    "ArbiterResult",
    "ArbiterPolicy",
]

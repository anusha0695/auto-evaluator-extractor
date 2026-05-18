"""
extractor.teams — Specialist Team compositions.

Phase 1 ships ONE team:

    MetadataTeam        — owns schema-v2 section `report_metadata`.
                          Composes Extractor → CoverageAuditor →
                          conditional Arbiter; emits a committed
                          report_metadata payload or an sme_flag verdict.

Phase 2 adds three more teams (GenomicVariantTeam, MolecularBiomarkerTeam,
TestedBiomarkerTeam). Same 3-agent skeleton, different schema section +
team-specific prompt template — the Team class is intentionally lean so
duplicating it costs ~20 LOC per team.
"""

from teams.metadata_team import MetadataTeam, MetadataTeamResult, TeamVerdict

__all__ = [
    "MetadataTeam",
    "MetadataTeamResult",
    "TeamVerdict",
]

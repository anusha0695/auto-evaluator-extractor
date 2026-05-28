"""
Section enable/disable toggle (v4-M1, Decision D3).

A declarative switch that lets a section's team be turned OFF without retiring its
code, prompt, or schema. Used by the v4 migration to disable `significant_findings`
(specimen_findings_team) and `clinical_information` (clinical_info_team) by default
while keeping them revivable — flip `enabled: true` in the team's config entry.

Mechanism: each team entry in the team registry (config/teams.yaml or a v4 registry)
MAY carry an `enabled: true|false` key. **Absent = enabled** (so existing configs and
the v3 path are unaffected — this is purely additive). The planner / graph team-set /
scorer / recall-floor consult `is_team_enabled` (wired in v4-M4) so a disabled team
never activates and its section is neither required nor scored.

Pure / offline — no imports beyond the stdlib so the gate can exercise it with plain
dicts.
"""

from __future__ import annotations

from typing import Any


def is_team_enabled(team_cfg: dict[str, Any] | None) -> bool:
    """True unless the team entry explicitly sets `enabled: false`.

    Absent key, None config, or any non-false value → enabled. Only a literal
    boolean ``False`` (or the strings 'false'/'no'/'off', case-insensitive) disables.
    """
    if not isinstance(team_cfg, dict):
        return True
    val = team_cfg.get("enabled", True)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() not in {"false", "no", "off", "0"}
    return bool(val)


def enabled_team_keys(teams_cfg: dict[str, Any] | None) -> list[str]:
    """The team keys whose entries are enabled, preserving registry order.

    `teams_cfg` is the parsed team registry (the mapping under the top-level `teams:`
    key, i.e. {team_key: {schema_section, ..., enabled?}})."""
    teams = (teams_cfg or {}).get("teams", teams_cfg) or {}
    if not isinstance(teams, dict):
        return []
    return [k for k, cfg in teams.items() if is_team_enabled(cfg)]


def filter_enabled_keys(keys: list[str], teams_cfg: dict[str, Any] | None) -> list[str]:
    """Subtractive: drop any team key whose registry entry is disabled, preserving
    order. Safe no-op when no team is disabled (e.g. the v3 teams.yaml), so wiring it
    into the team-set build does not change v2/v3 behavior."""
    teams = (teams_cfg or {}).get("teams", teams_cfg) or {}
    return [k for k in keys if is_team_enabled(teams.get(k) if isinstance(teams, dict) else None)]


def disabled_sections(teams_cfg: dict[str, Any] | None) -> set[str]:
    """The set of `schema_section` names whose owning team is disabled — so the
    schema validator / scorer / recall-floor can skip them. Honors both the nested
    `{teams: {...}}` shape and a bare `{team_key: {...}}` mapping."""
    teams = (teams_cfg or {}).get("teams", teams_cfg) or {}
    out: set[str] = set()
    if isinstance(teams, dict):
        for cfg in teams.values():
            if isinstance(cfg, dict) and not is_team_enabled(cfg):
                sec = cfg.get("schema_section")
                if sec:
                    out.add(str(sec))
    return out

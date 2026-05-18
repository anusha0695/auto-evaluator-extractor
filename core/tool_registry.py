"""
Tool registry — builds LangChain tools from `config/tools.yaml` and binds
them to a `PipelineState` via closure factories.

The 7 Phase 1 tools and their handlers:

    pdf_page_loader        — reads page text + blocks from state.doc_profile
    pdf_text_search        — case-insensitive substring search across pages
    docai_layout_lookup    — looks up a block by (page_number, section_path)
    npi_validator          — Luhn-mod-10 check (CMS NPI standard)
    date_parser            — wraps dateutil.parser, returns ISO YYYY-MM-DD
    schema_validate        — runs SchemaLoader.validate on a candidate
    state_read             — whitelisted state-key read (no mutation)

Usage by an agent (Block E):

    tools = build_tools_for_state(
        state=current_state,
        allowlist=teams_yaml["metadata_team"]["tool_allowlist"],
        schema_loader=schema_loader,
    )
    llm_with_tools = llm.bind_tools(tools)
    ...

Closure pattern ensures each ReAct loop gets fresh tools bound to its run's
state. No global state mutation, no race conditions between concurrent docs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from core.errors import ToolError
from core.observability import trace
from core.schema_loader import SchemaLoader, UmbrellaSection
from core.state import PipelineState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closure-based factory
# ---------------------------------------------------------------------------


def build_tools_for_state(
    *,
    state: PipelineState,
    allowlist: list[str],
    schema_loader: SchemaLoader | None = None,
) -> list[StructuredTool]:
    """Return a list of LangChain `StructuredTool`s bound to this run's state.

    Args:
        state: the current PipelineState — captured in tool closures.
        allowlist: tool names from `teams.yaml` `tool_allowlist`. Any tool
            not in this list is excluded from the returned bundle.
        schema_loader: required if the team is allowed to call
            `schema_validate`. Ignored otherwise.

    Returns:
        List of `StructuredTool`s, in the order they appear in the allowlist.
        Raises ToolError if an unknown tool name appears in the allowlist.
    """
    allowed = set(allowlist)
    unknown = allowed - _ALL_TOOL_NAMES
    if unknown:
        raise ToolError(
            f"Unknown tool(s) in allowlist: {sorted(unknown)}. "
            f"Valid tools: {sorted(_ALL_TOOL_NAMES)}",
            retry_safe=False,
            context={"unknown": sorted(unknown)},
        )

    tools: list[StructuredTool] = []
    if "pdf_page_loader" in allowed:
        tools.append(_make_pdf_page_loader(state))
    if "pdf_text_search" in allowed:
        tools.append(_make_pdf_text_search(state))
    if "docai_layout_lookup" in allowed:
        tools.append(_make_docai_layout_lookup(state))
    if "npi_validator" in allowed:
        tools.append(_make_npi_validator())
    if "date_parser" in allowed:
        tools.append(_make_date_parser())
    if "schema_validate" in allowed:
        if schema_loader is None:
            raise ToolError(
                "schema_validate is in the allowlist but no schema_loader was passed",
                retry_safe=False,
            )
        tools.append(_make_schema_validate(schema_loader))
    if "state_read" in allowed:
        tools.append(_make_state_read(state))

    return tools


_ALL_TOOL_NAMES: set[str] = {
    "pdf_page_loader",
    "pdf_text_search",
    "docai_layout_lookup",
    "npi_validator",
    "date_parser",
    "schema_validate",
    "state_read",
}


# ---------------------------------------------------------------------------
# Input schemas (Pydantic — LangChain reads these to build the tool's args spec)
# ---------------------------------------------------------------------------


class PdfPageLoaderInput(BaseModel):
    page_number: int = Field(..., ge=1, description="1-indexed page number.")


class PdfTextSearchInput(BaseModel):
    query: str = Field(..., min_length=1, description="Substring (case-insensitive).")
    max_results: int = Field(5, ge=1, le=50, description="Cap on matches returned.")


class DocaiLayoutLookupInput(BaseModel):
    page_number: int = Field(..., ge=1, description="1-indexed page number.")
    section_path: str | None = Field(
        None, description="Optional DocAI section path filter (e.g. 'page_1/header')."
    )


class NpiValidatorInput(BaseModel):
    npi: str = Field(..., min_length=10, max_length=10, description="Exactly 10 digits.")


class DateParserInput(BaseModel):
    raw_date: str = Field(..., min_length=1, description="Date string as printed.")


class SchemaValidateInput(BaseModel):
    section: str = Field(..., description="Umbrella section name (e.g. 'report_metadata').")
    candidate: dict[str, Any] = Field(..., description="Candidate JSON to validate.")


class StateReadInput(BaseModel):
    key: str = Field(..., description="One of 'doc_profile', 'parser_hypothesis', 'block_profiles'.")


# ---------------------------------------------------------------------------
# Tool factories — one per tool, each closing over the state it needs
# ---------------------------------------------------------------------------


def _make_pdf_page_loader(state: PipelineState) -> StructuredTool:
    @trace("core.tool_registry.pdf_page_loader")
    def _handler(page_number: int) -> dict[str, Any]:
        doc_profile = state.get("doc_profile")
        if not doc_profile:
            raise ToolError(
                "pdf_page_loader: state.doc_profile is missing — preprocessing did not run",
                context={"tool": "pdf_page_loader"},
            )
        pages = doc_profile.get("pages", []) or []
        for p in pages:
            if p.get("page_number") == page_number:
                block_ids = p.get("block_ids_on_page", [])
                blocks = [
                    b for b in (doc_profile.get("blocks") or [])
                    if b.get("block_id") in set(block_ids)
                ]
                return {
                    "page_number": page_number,
                    "text": p.get("text", ""),
                    "blocks": blocks,
                }
        raise ToolError(
            f"pdf_page_loader: page {page_number} not in doc_profile (total_pages="
            f"{doc_profile.get('total_pages')})",
            context={"tool": "pdf_page_loader", "page_number": page_number},
        )

    return StructuredTool.from_function(
        func=_handler,
        name="pdf_page_loader",
        description=(
            "Return the rendered text of a specific page from the source PDF, plus the "
            "list of DocAI layout blocks on that page (each with bbox and section_path)."
        ),
        args_schema=PdfPageLoaderInput,
    )


def _make_pdf_text_search(state: PipelineState) -> StructuredTool:
    @trace("core.tool_registry.pdf_text_search")
    def _handler(query: str, max_results: int = 5) -> dict[str, Any]:
        doc_profile = state.get("doc_profile")
        if not doc_profile:
            raise ToolError("pdf_text_search: state.doc_profile is missing")
        pat = re.compile(re.escape(query), re.IGNORECASE)
        matches: list[dict[str, Any]] = []
        for p in doc_profile.get("pages", []) or []:
            text = p.get("text", "") or ""
            for m in pat.finditer(text):
                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 40)
                matches.append({
                    "page_number": p.get("page_number"),
                    "match_start": m.start(),
                    "match_end": m.end(),
                    "excerpt": text[start:end],
                })
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break
        return {"query": query, "match_count": len(matches), "matches": matches}

    return StructuredTool.from_function(
        func=_handler,
        name="pdf_text_search",
        description=(
            "Case-insensitive substring search across every page of the source PDF. "
            "Returns up to N matches with surrounding excerpts."
        ),
        args_schema=PdfTextSearchInput,
    )


def _make_docai_layout_lookup(state: PipelineState) -> StructuredTool:
    @trace("core.tool_registry.docai_layout_lookup")
    def _handler(page_number: int, section_path: str | None = None) -> dict[str, Any]:
        doc_profile = state.get("doc_profile")
        if not doc_profile:
            raise ToolError("docai_layout_lookup: state.doc_profile is missing")
        blocks = [
            b for b in (doc_profile.get("blocks") or [])
            if b.get("page_number") == page_number
            and (section_path is None or b.get("section_path", "").startswith(section_path))
        ]
        return {"page_number": page_number, "section_path": section_path, "blocks": blocks}

    return StructuredTool.from_function(
        func=_handler,
        name="docai_layout_lookup",
        description=(
            "Return DocAI layout blocks for a given page, optionally filtered by section_path. "
            "Useful for inspecting structural context."
        ),
        args_schema=DocaiLayoutLookupInput,
    )


def _make_npi_validator() -> StructuredTool:
    @trace("core.tool_registry.npi_validator")
    def _handler(npi: str) -> dict[str, Any]:
        npi = npi.strip()
        if not (npi.isdigit() and len(npi) == 10):
            return {"npi": npi, "valid": False, "reason": "Not exactly 10 digits"}
        # CMS NPI uses Luhn-mod-10 over the prefix "80840" + 10-digit NPI.
        full = "80840" + npi
        if _luhn_valid(full):
            return {"npi": npi, "valid": True, "reason": "Luhn check passed"}
        return {"npi": npi, "valid": False, "reason": "Luhn check failed"}

    return StructuredTool.from_function(
        func=_handler,
        name="npi_validator",
        description=(
            "Validate a 10-digit NPI using the CMS Luhn-mod-10 algorithm (with the "
            "standard '80840' issuer prefix). Returns valid=true|false with a reason."
        ),
        args_schema=NpiValidatorInput,
    )


def _luhn_valid(s: str) -> bool:
    """Luhn mod-10 check for a digit string."""
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _make_date_parser() -> StructuredTool:
    @trace("core.tool_registry.date_parser")
    def _handler(raw_date: str) -> dict[str, Any]:
        try:
            from dateutil import parser as _dateparser
        except ImportError as exc:
            raise ToolError(f"date_parser requires python-dateutil: {exc}") from exc

        # Strip trailing sex / TZ fragments that often follow a date in
        # genomic reports, e.g. "01/25/1941 / M" or "08/13/2025 01:15 PM PDT".
        cleaned = re.sub(r"\s*/\s*[MFmfUu]\s*$", "", raw_date.strip())
        try:
            dt = _dateparser.parse(cleaned, default=None, fuzzy=True)
        except (ValueError, OverflowError, TypeError) as exc:
            return {"raw": raw_date, "iso": None, "ok": False, "reason": str(exc)}
        if dt is None:
            return {"raw": raw_date, "iso": None, "ok": False, "reason": "could not parse"}
        return {"raw": raw_date, "iso": dt.date().isoformat(), "ok": True}

    return StructuredTool.from_function(
        func=_handler,
        name="date_parser",
        description=(
            "Parse a date string in any common format and return its ISO YYYY-MM-DD form. "
            "Strips time-of-day, timezone, and trailing sex fragments ('/ M', '/ F')."
        ),
        args_schema=DateParserInput,
    )


def _make_schema_validate(schema_loader: SchemaLoader) -> StructuredTool:
    @trace("core.tool_registry.schema_validate")
    def _handler(section: str, candidate: dict[str, Any]) -> dict[str, Any]:
        from typing import get_args as _get_args

        if section not in _get_args(UmbrellaSection):
            return {
                "ok": False,
                "section": section,
                "errors": [f"Unknown section. Valid: {list(_get_args(UmbrellaSection))}"],
            }
        try:
            schema_loader.validate(section, candidate)  # type: ignore[arg-type]
            return {"ok": True, "section": section, "errors": []}
        except Exception as exc:  # SchemaValidationError carries .context["errors"]
            ctx = getattr(exc, "context", {}) or {}
            return {
                "ok": False,
                "section": section,
                "errors": ctx.get("errors", [str(exc)]),
            }

    return StructuredTool.from_function(
        func=_handler,
        name="schema_validate",
        description=(
            "Run the schema-v2 Pydantic validator against a candidate dict for the named "
            "umbrella section. Returns ok=true|false with field-level errors."
        ),
        args_schema=SchemaValidateInput,
    )


def _make_state_read(state: PipelineState) -> StructuredTool:
    ALLOWED_KEYS = {"doc_profile", "parser_hypothesis", "block_profiles"}

    @trace("core.tool_registry.state_read")
    def _handler(key: str) -> dict[str, Any]:
        if key not in ALLOWED_KEYS:
            raise ToolError(
                f"state_read: key {key!r} not in allowlist {sorted(ALLOWED_KEYS)}",
                context={"tool": "state_read", "key": key},
            )

        # "block_profiles" lives under doc_profile.block_profiles
        if key == "block_profiles":
            doc_profile = state.get("doc_profile") or {}
            return {"key": key, "value": doc_profile.get("block_profiles", [])}

        return {"key": key, "value": state.get(key)}

    return StructuredTool.from_function(
        func=_handler,
        name="state_read",
        description=(
            "Read one of the whitelisted PipelineState keys: 'doc_profile', "
            "'parser_hypothesis', 'block_profiles'."
        ),
        args_schema=StateReadInput,
    )


# ---------------------------------------------------------------------------
# Helper for building ToolDescriptors (consumed by PromptRenderer)
# ---------------------------------------------------------------------------


def descriptors_from_yaml(
    tools_yaml_path: str = "config/tools.yaml",
    allowlist: list[str] | None = None,
) -> list[Any]:
    """Read `tools.yaml` and return `ToolDescriptor`s for prompt rendering.

    This is the bridge between the tool registry (which builds runnable
    LangChain tools) and the prompt renderer (which only needs the
    name + description + input_schema to render the prompt).
    """
    import yaml

    from core.prompt_renderer import ToolDescriptor

    raw = yaml.safe_load(open(tools_yaml_path, encoding="utf-8"))
    out: list[ToolDescriptor] = []
    for name, entry in raw.get("tools", {}).items():
        if allowlist is not None and name not in allowlist:
            continue
        out.append(
            ToolDescriptor(
                name=name,
                description=entry.get("description", "").strip(),
                input_schema=entry.get("input_schema", {}),
            )
        )
    return out

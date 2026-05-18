"""
extractor.preprocess — preprocessing layer.

Runs once per document. Produces the inputs every downstream agent reads from:

    DocAIParser          — wraps DocAI Layout Parser; returns DocProfile.
    FaxHeaderFilter      — deterministic regex stripper for fax-transport noise.
    BlockProfiler        — Gemini 2.5 Flash @ T=0.0 semantic block classifier.
    SciSpaCyMedicalNER   — in-process SciSpaCy + Gemini Flash post-processor;
                            emits ParserHypothesis for Coverage Auditors.
    PreprocessNode       — LangGraph node that wires the four together.

Output flow per document:

    PDF (GCS)
       │
       ▼
    DocAIParser.parse()           ─► DocProfile.blocks, DocProfile.pages (raw)
       │
       ▼
    FaxHeaderFilter.filter()      ─► DocProfile.pages with fax bands stripped
       │
       ├─► BlockProfiler.profile()  ─► DocProfile.block_profiles (text_role tags)
       │                                  (incl. surviving fax noise tagged
       │                                   `fax_transport_noise`)
       │
       └─► SciSpaCyMedicalNER.run() ─► ParserHypothesis (typed entity candidates)

The two branches run concurrently via asyncio.gather inside PreprocessNode.
All-or-nothing: any failure routes the doc to SME (no partial state).
"""

from preprocess.block_profiler import BlockProfiler
from preprocess.docai_parser import DocAIParser
from preprocess.fax_header_filter import FaxHeaderFilter
from preprocess.medical_ner import SciSpaCyMedicalNER
from preprocess.preprocess_node import (
    PreprocessNodeDependencies,
    make_preprocess_node,
    run_preprocess,
)

__all__ = [
    "DocAIParser",
    "FaxHeaderFilter",
    "BlockProfiler",
    "SciSpaCyMedicalNER",
    "PreprocessNodeDependencies",
    "make_preprocess_node",
    "run_preprocess",
]

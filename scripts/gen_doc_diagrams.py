"""
Generate the documentation flowchart images (PNG) with Graphviz.

Renders into docs/architecture/images/. Re-run after changing the flow:

    PYTHONPATH=. python scripts/gen_doc_diagrams.py

Labels are intentionally written in PLAIN LANGUAGE so someone new to the project can
follow the diagrams without knowing the code. The precise function / identifier names
live in the prose docs (architecture/overview.md, components.md, loops.md) and in the
traceability table — not in the pictures.

This is docs tooling (not a pipeline component, not a gate). The diagrams it produces
are embedded by docs/architecture/diagrams.md and docs/architecture/loops.md.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import graphviz

OUT = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "images"
OUT.mkdir(parents=True, exist_ok=True)
_TMP = Path(tempfile.mkdtemp(prefix="docdiag_"))

# ---- shared styling --------------------------------------------------------
PROC = dict(shape="box", style="rounded,filled", fillcolor="#eef2ff", color="#6366f1", fontname="Helvetica", fontsize="11")
DEC = dict(shape="diamond", style="filled", fillcolor="#fef9c3", color="#ca8a04", fontname="Helvetica", fontsize="10")
TERM = dict(shape="box", style="rounded,filled", fillcolor="#dcfce7", color="#16a34a", fontname="Helvetica", fontsize="11")
ESC = dict(shape="box", style="rounded,filled", fillcolor="#fee2e2", color="#dc2626", fontname="Helvetica", fontsize="11")
IO = dict(shape="box", style="filled", fillcolor="#f1f5f9", color="#475569", fontname="Helvetica", fontsize="10")


def _g(name: str, rankdir: str = "TB") -> graphviz.Digraph:
    g = graphviz.Digraph(name, format="png")
    g.attr(rankdir=rankdir, bgcolor="white", fontname="Helvetica")
    g.attr("edge", fontname="Helvetica", fontsize="9", color="#475569")
    return g


def render(g: graphviz.Digraph, fname: str) -> None:
    p = Path(g.render(filename=fname, directory=str(_TMP), cleanup=True))
    dest = OUT / p.name
    shutil.copyfile(p, dest)
    print("wrote", dest.relative_to(OUT.parents[2]))


def _make_icons() -> dict[str, str]:
    """Draw simple, self-contained icons (no web logos) for the system-design diagram:
    an LLM-agent glyph and an NLP-model glyph. Returns {name: path}. Used as Custom()
    node images by system_design()."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, FancyBboxPatch

    idir = _TMP / "icons"
    idir.mkdir(exist_ok=True)
    agent = str(idir / "agent.png")
    model = str(idir / "model.png")

    # --- LLM agent: a friendly robot head with an antenna + chat sparkle ---
    fig, ax = plt.subplots(figsize=(2.6, 2.6), dpi=110)
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    ax.plot([50, 50], [80, 90], color="#4338ca", lw=3, solid_capstyle="round")
    ax.add_patch(Circle((50, 92), 4.5, color="#4338ca"))
    ax.add_patch(FancyBboxPatch((20, 30), 60, 48, boxstyle="round,pad=2,rounding_size=12",
                                fc="#6366f1", ec="#4338ca", lw=2.5))
    for ex in (38, 62):                       # eyes
        ax.add_patch(Circle((ex, 58), 6.5, color="white"))
        ax.add_patch(Circle((ex, 58), 2.8, color="#1e1b4b"))
    ax.plot([40, 60], [44, 44], color="white", lw=3, solid_capstyle="round")  # mouth
    ax.text(83, 74, "✨", fontsize=15, color="#facc15", ha="center", va="center")  # LLM sparkle
    fig.savefig(agent, transparent=True, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)

    # --- NLP model: a small neural-net (3-2-1) glyph ---
    fig, ax = plt.subplots(figsize=(2.6, 2.6), dpi=110)
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    cols = {22: [25, 50, 75], 52: [37, 63], 80: [50]}
    pts = {x: [(x, y) for y in ys] for x, ys in cols.items()}
    xs = sorted(cols)
    for xa, xb in zip(xs, xs[1:]):            # edges
        for pa in pts[xa]:
            for pb in pts[xb]:
                ax.plot([pa[0], pb[0]], [pa[1], pb[1]], color="#5eead4", lw=1.2, zorder=1)
    for x in xs:                              # nodes
        for (px, py) in pts[x]:
            ax.add_patch(Circle((px, py), 7, fc="#0d9488", ec="#115e59", lw=1.5, zorder=2))
    fig.savefig(model, transparent=True, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    return {"agent": agent, "model": model}


# ===========================================================================
# 0. Overall pipeline — horizontal main line; ping-back loop + VMAW + SME shown;
#    each stage lists the feature it brings; a "key capabilities" band underneath.
# ===========================================================================
def overall() -> None:
    g = _g("overall", rankdir="LR")
    g.attr(nodesep="0.4", ranksep="0.7")
    g.attr("node", margin="0.16,0.10")

    # --- main pipeline line ---
    g.node("pre", "Read the PDF\n• blocks · layout · tables\n• candidate entities (NER floor)", **PROC)
    g.node("plan", "Plan\n• pick which sections to run", **PROC)
    g.node("teams", "Specialist teams x5\n• Extractor -> Auditor -> Arbiter\n• re-extract once (agentic loop)", **PROC)
    g.node("link", "Linker\n• assemble nested record\n• typed relationships + supersession", **PROC)
    g.node("ver", "Verifiers - 8 checks\n• structure · coverage · recall\n• evidence · ownership · normalization\n• binding V1-V4", **PROC)
    g.node("tri", "Triage\nany problem\nthat is auto-fixable?", **DEC)
    g.node("vmaw", "VMAW deep-resolve\n• widen context\n• find the exact quote\n• pick the best value", **PROC)
    g.node("dec", "Decide outcome\n• accept / partial / review", **PROC)
    g.node("save", "Save results + artifacts\n(+ production JSON)", **TERM)

    # --- the two off-line nodes: the repair loop and the human queue ---
    g.node("rep", "AUTO-FIX  (ping-back)\n• re-extract section · re-route block\n• re-connect · re-normalize · drop", **PROC)
    g.node("sme", "Human-review queue (SME)\nonly genuinely ambiguous items", **ESC)

    g.edge("pre", "plan", label="blocks +\ncandidates")
    g.edge("plan", "teams", label="sections")
    g.edge("teams", "link", label="extracted\nsections")
    g.edge("link", "ver", label="nested record\n+ connections")
    g.edge("ver", "tri", label="pass / fail")

    # ping-back / repair loop — bold, distinct colour, returns to the linker
    g.edge("tri", "rep", label="YES — fixable\n(PING-BACK)", color="#d97706", fontcolor="#b45309",
           penwidth="2")
    g.edge("rep", "link", label="patched -> re-run checks\n(bounded: repeats & budget escalate)",
           color="#d97706", fontcolor="#b45309", penwidth="2", constraint="false")

    # exit the loop -> VMAW -> decide -> save; escalations to SME
    g.edge("tri", "vmaw", label="NO — hand the\nreview queue over", color="#16a34a")
    g.edge("vmaw", "dec", label="resolved /\nheld items")
    g.edge("vmaw", "sme", label="still needs\na human", color="#dc2626", style="dashed")
    g.edge("dec", "save", label="auto / partial")
    g.edge("dec", "sme", label="send to review", color="#dc2626")

    # --- "what makes this pipeline different" band ---
    with g.subgraph(name="cluster_feat") as c:
        c.attr(label="Key capabilities (cross-cutting)", style="rounded,filled",
               fillcolor="#f8fafc", color="#94a3b8", fontname="Helvetica", fontsize="11")
        c.node("feat",
               "• Every value is evidence-grounded with per-field provenance (block + quote + why)\l"
               "• Self-correction is BOUNDED — the ping-back loop always terminates (recur-guard + budget caps)\l"
               "• Escalate to a human ONLY for genuine ambiguity (clean non-matches are dropped, not queued)\l"
               "• Deterministic gates lock every milestone · temperature = 0.0 everywhere\l"
               "• All model calls run on Vertex AI Gemini (BAA) — PHI never leaves the project\l",
               shape="box", style="filled", fillcolor="white", color="#cbd5e1",
               fontname="Helvetica", fontsize="10")
    render(g, "overall_pipeline")


# ===========================================================================
# 1. Read the PDF (preprocess)
# ===========================================================================
def preprocess() -> None:
    g = _g("preprocess")
    g.node("pdf", "PDF report", **IO)
    g.node("docai", "Parse the PDF\n(text blocks + positions + tables)", **PROC)
    g.node("stitch", "Join tables split across pages", **PROC)
    g.node("fax", "Remove fax / header noise", **PROC)
    g.node("bbox", "Fill in any missing positions", **PROC)
    g.node("geo", "Record each word's position\n(for highlighting later)", **PROC)
    g.node("prof", "Label each block\n(what kind of text,\nwhich section it belongs to)", **PROC)
    g.node("ner", "Scan for candidate entities\n(the recall safety net)", **PROC)
    g.node("out", "Clean blocks + candidate entities\n+ word positions", **IO)
    g.edge("pdf", "docai"); g.edge("docai", "stitch"); g.edge("docai", "fax")
    g.edge("stitch", "bbox"); g.edge("bbox", "geo"); g.edge("fax", "prof")
    g.edge("prof", "ner"); g.edge("geo", "out"); g.edge("prof", "out"); g.edge("ner", "out")
    render(g, "preprocess")


# ===========================================================================
# 2. Choose sections (planner)
# ===========================================================================
def planner() -> None:
    g = _g("planner")
    g.node("sig", "Labeled blocks + candidate entities", **IO)
    g.node("plan", "For each section...", **PROC)
    g.node("q", "Any blocks\nfor this section?", **DEC)
    g.node("act", "Extract this section", **PROC)
    g.node("skip", "Skip this section", **PROC)
    g.node("out", "Sections to extract", **IO)
    g.edge("sig", "plan"); g.edge("plan", "q")
    g.edge("q", "act", label="yes", color="#16a34a"); g.edge("q", "skip", label="no", color="#dc2626")
    g.edge("act", "out"); g.edge("skip", "out")
    render(g, "planner")


# ===========================================================================
# 3. Agentic loop (in-team) — plain language, with conditions
# ===========================================================================
def agentic_loop() -> None:
    g = _g("agentic_loop")
    g.node("in", "The section's text blocks\n+ candidate entities", **IO)
    g.node("ex", "Extractor\nreads the page and pulls the fields", **PROC)
    g.node("au", "Auditor\nchecks: did we miss or add\nanything vs the candidate list?", **PROC)
    g.node("gap", "Coverage looks off?\n(too few or extra fields)", **DEC)
    g.node("ar", "Arbiter\ndecides what to re-check\nand gives targeted hints", **PROC)
    g.node("rx", "Extractor re-runs ONCE\nwith those hints", **PROC)
    g.node("out", "Finished section\n(+ step-by-step trace)", **TERM)
    g.edge("in", "ex"); g.edge("ex", "au"); g.edge("au", "gap")
    g.edge("gap", "out", label="no - looks complete -> accept", color="#16a34a")
    g.edge("gap", "ar", label="yes - needs another look", color="#ca8a04")
    g.edge("ar", "rx", label="hints")
    g.edge("rx", "out", label="single pass (no second redo)")
    render(g, "agentic_loop")


# ===========================================================================
# 4. Connect related items (linker)
# ===========================================================================
def linker() -> None:
    g = _g("linker")
    g.node("so", "Each team's extracted section", **IO)
    g.node("asm", "Combine into one nested record", **PROC)
    g.node("reg", "Find related items\n(known relationship types)", **PROC)
    g.node("det", "Obvious match?\n(e.g. same gene / id)", **DEC)
    g.node("trust", "Accept the connection", **PROC)
    g.node("adj", "Allowed to ask the AI\nto confirm?", **DEC)
    g.node("conf", "AI confirms the connection", **PROC)
    g.node("unc", "Unsure -> send to review", **ESC)
    g.node("sup", "Newer / amended value wins\n(older value kept on record)", **PROC)
    g.node("out", "One nested record + connections", **IO)
    g.edge("so", "asm"); g.edge("asm", "reg"); g.edge("reg", "det")
    g.edge("det", "trust", label="yes", color="#16a34a")
    g.edge("det", "adj", label="no")
    g.edge("adj", "conf", label="yes"); g.edge("adj", "unc", label="no")
    g.edge("trust", "sup"); g.edge("conf", "sup"); g.edge("unc", "sup"); g.edge("sup", "out")
    render(g, "linker")


# ===========================================================================
# 5. Quality checks (verifier suite)
# ===========================================================================
def verifiers() -> None:
    g = _g("verifiers")
    g.node("env", "The nested record + connections", **IO)
    seq = [
        ("v1", "Structure is valid?"),
        ("v2", "Coverage: nothing important missed?"),
        ("v3", "Connections make sense?"),
        ("v4", "Each value has supporting evidence?"),
        ("v5", "Recall floor: blocks that should\nyield a field actually did?"),
        ("v6", "Ownership: each detail tied\nto the right owner?"),
        ("v7", "Cleaned-up value matches\nthe original wording?"),
        ("v8", "Evidence checks:\n- value really in the text?\n- belongs to THIS test?\n- nothing made up?\n- connection supported?"),
    ]
    for nid, lbl in seq:
        g.node(nid, lbl, **PROC)
    g.node("out", "List of checks (passed / failed)", **IO)
    g.edge("env", "v1")
    for (a, _), (b, _) in zip(seq, seq[1:]):
        g.edge(a, b)
    g.edge("v8", "out")
    render(g, "verifier_suite")


# ===========================================================================
# 6. Ping-back / repair loop — plain language, with all conditions
# ===========================================================================
def pingback_loop() -> None:
    g = _g("pingback")
    # --- input ---
    g.node("in", "INPUT\nFailed quality checks\n(from the verifiers)", **IO)
    g.node("bd", "Turn each failure\ninto a specific problem", **PROC)
    # --- per-problem decision ladder (any YES -> escalate) ---
    g.node("esconly", "Needs human judgment?\n(uncertain match /\nflagged for review)", **DEC)
    g.node("noteam", "Fix needs a section\nbut none can be assigned?", **DEC)
    g.node("recur", "Already tried fixing\nthis exact problem once?", **DEC)
    g.node("cap", "Out of retry budget?\n(this section, or overall\n= sections x 2)", **DEC)
    g.node("router", "Reviewer-AI says\nre-running won't fix it?", **DEC)
    g.node("fixq", "Safe to auto-fix\nthis round", **PROC)
    g.node("queue", "Human-review queue\n(duplicates removed)", **ESC)
    # --- apply + the bounded loop ---
    g.node("act", "Pick a fix: re-extract section /\nre-route block / re-connect /\nre-normalize value", **PROC)
    g.node("apply", "Apply fixes, re-assemble,\nRE-RUN all quality checks", **PROC)
    g.node("more", "Were any fixes\napplied this round?", **DEC)
    # --- termination + output ---
    g.node("vmaw", "VMAW: deep-resolve the\nreview queue\n(widen context / find the quote /\npick the best value)", **PROC)
    g.node("out", "OUTPUT ->\nDecide outcome -> Save", **TERM)
    g.node("guard", "Why it always ends:\na repeated problem OR\nhitting the budget is escalated,\nnot retried — so each round\nshrinks the fixable set.",
           shape="note", style="filled", fillcolor="#f8fafc", color="#94a3b8", fontsize="9", fontname="Helvetica")

    g.edge("in", "bd"); g.edge("bd", "esconly", label="per problem")
    g.edge("esconly", "queue", label="yes", color="#dc2626")
    g.edge("esconly", "noteam", label="no")
    g.edge("noteam", "queue", label="yes", color="#dc2626")
    g.edge("noteam", "recur", label="no")
    g.edge("recur", "queue", label="yes - escalate", color="#dc2626")
    g.edge("recur", "cap", label="no")
    g.edge("cap", "queue", label="yes - escalate", color="#dc2626")
    g.edge("cap", "router", label="no")
    g.edge("router", "queue", label="yes", color="#dc2626")
    g.edge("router", "fixq", label="no", color="#16a34a")
    g.edge("fixq", "act"); g.edge("act", "apply"); g.edge("apply", "more")
    g.edge("more", "in", label="yes -> next round\n(re-check everything)", color="#ca8a04")
    g.edge("more", "vmaw", label="no -> loop ends", color="#16a34a")
    g.edge("queue", "vmaw", label="hand off the queue", style="dashed")
    g.edge("vmaw", "out")
    g.edge("guard", "more", style="dotted", color="#94a3b8", arrowhead="none")
    render(g, "pingback_repair_loop")


# ===========================================================================
# 7. Deep-resolve (VMAW)
# ===========================================================================
def vmaw() -> None:
    g = _g("vmaw")
    g.node("q", "An item sent for resolution", **IO)
    g.node("route", "Choose tactics for\nthis kind of doubt", **PROC)
    g.node("cap", "Try in order:\nwiden the context ->\nfind the exact quote ->\npick the best value", **PROC)
    g.node("r", "What happened?", **DEC)
    g.node("rechk", "Independently checks out?", **DEC)
    g.node("apply", "Confirm it automatically\n(do not overwrite)", **TERM)
    g.node("hold", "Hold for human review", **ESC)
    g.node("drop", "Unsupported -> remove from record\n(kept in the queue for audit)", **ESC)
    g.node("flag", "Leave flagged for review", **ESC)
    g.edge("q", "route"); g.edge("route", "cap"); g.edge("cap", "r")
    g.edge("r", "rechk", label="found solid evidence")
    g.edge("rechk", "apply", label="yes", color="#16a34a")
    g.edge("rechk", "hold", label="no", color="#dc2626")
    g.edge("r", "hold", label="conflicting values", color="#dc2626")
    g.edge("r", "drop", label="no support found", color="#dc2626")
    g.edge("r", "flag", label="still unresolved", color="#dc2626")
    render(g, "vmaw")


# ===========================================================================
# 8. Decide + save
# ===========================================================================
def decision() -> None:
    g = _g("decision")
    g.node("in", "Team results + checks + confidence", **IO)
    g.node("q1", "Anything flagged, failed,\nor low confidence?", **DEC)
    g.node("q2", "Some sections clean,\nothers need review?", **DEC)
    g.node("aa", "Accept automatically", **TERM)
    g.node("pa", "Accept the clean parts,\nreview the rest", **PROC)
    g.node("sme", "Send to human review", **ESC)
    g.node("p", "Save results + review queue", **PROC)
    g.edge("in", "q1")
    g.edge("q1", "q2", label="yes")
    g.edge("q1", "aa", label="no", color="#16a34a")
    g.edge("q2", "pa", label="yes")
    g.edge("q2", "sme", label="no", color="#dc2626")
    g.edge("aa", "p"); g.edge("pa", "p"); g.edge("sme", "p")
    render(g, "decision_persist")


# ===========================================================================
# 9. System design (deployment) — real GCP + Vertex / Python icons
# ===========================================================================
def system_design() -> None:
    """Infrastructure / service view using the `diagrams` icon library (mingrammer).
    Shows the actual GCP services and where the agent runtime (LangGraph on Vertex AI
    Gemini) sits. Rendered to /tmp then copied (avoids mounted-fs cleanup issues)."""
    from diagrams import Cluster, Diagram, Edge
    from diagrams.custom import Custom
    from diagrams.gcp.analytics import BigQuery
    from diagrams.gcp.database import Firestore
    from diagrams.gcp.ml import AIPlatform, VertexAI
    from diagrams.gcp.operations import Logging, Monitoring
    from diagrams.gcp.storage import GCS
    from diagrams.onprem.client import Client, User
    from diagrams.programming.flowchart import Decision

    ic = _make_icons()
    AG = ic["agent"]    # LLM-agent glyph
    ML = ic["model"]    # NLP-model glyph

    base = str(_TMP / "system_design")
    with Diagram(
        "Extractor — System Design (GCP services + agentic runtime)", filename=base,
        outformat="png", show=False, direction="LR",
        graph_attr={"bgcolor": "white", "fontname": "Helvetica", "fontsize": "18",
                    "pad": "0.5", "nodesep": "0.40", "ranksep": "1.1", "splines": "spline"},
    ):
        src = GCS("Input PDFs\n(GCS bucket)")

        with Cluster("1 · Ingestion (GCP)"):
            docai = AIPlatform("Document AI\nLayout Parser\n(blocks · bbox · tables)")

        with Cluster("2 · Preprocess models (in-worker)"):
            ner = Custom("Medical NER floor\nSciSpaCy / MedSpaCy", ML)
            prof = Custom("Block profiler\n(roles + routing)", ML)

        with Cluster("Shared tools (deterministic)"):
            hgnc = Custom("HGNC resolver\n(gene symbols)", ML)
            hgvs = Custom("HGVS validator\n(variant notation)", ML)
            negn = Custom("Negation / ConText", ML)
            norm = Custom("Normalizers\n(date · method · biomarker)", ML)

        with Cluster("3 · Agentic runtime — LangGraph graph_selfcorrecting"):
            with Cluster("5 specialist teams (LLM agents)"):
                teams = Custom("Extractor -> Coverage Auditor\n-> Arbiter -> re-extract\n(metadata · biomarker · tested\n· specimen · clinical)", AG)
            planner = Custom("Planner\n(activate teams)", AG)
            linker = Custom("Linker\n(typed links + supersession)", AG)
            verif = Custom("Verifier suite\n8 checks + binding V1-V4", AG)
            triage = Decision("Triage + Repair\nping-back controller")
            vmaw = Custom("VMAW\nEC · CITE · VA", AG)
            adj = Custom("LLM adjudicator hooks\n(gated)", AG)
            router = Custom("Decision router\n(accept / partial / SME)", AG)

        with Cluster("4 · Persistence & Observability (in-project)"):
            ckpt = Firestore("Firestore\ncheckpoints")
            bq = BigQuery("BigQuery\nruns + extractions")
            art = GCS("GCS\nartifacts +\nproduction JSON")
            logs = Logging("Cloud Logging")
            mon = Monitoring("Cloud Monitoring\n/ Trace")

        gemini = VertexAI("Vertex AI — Gemini 2.5\n(every LLM call · BAA · T=0.0)")
        ui = Client("Streamlit\nSME review UI")
        sme = User("SME reviewer")

        # --- main data flow ---
        src >> docai
        docai >> ner; docai >> prof
        ner >> planner; prof >> planner
        planner >> teams >> linker >> verif >> triage
        triage >> vmaw >> router

        # --- the ping-back / repair loop can re-invoke ANY of these ---
        triage >> Edge(label="re-extract / reprofile", color="#d97706", style="bold", fontcolor="#b45309") >> teams
        triage >> Edge(label="re-connect", color="#d97706", style="bold", fontcolor="#b45309") >> linker
        triage >> Edge(label="re-route block", color="#d97706", style="bold", fontcolor="#b45309") >> prof
        triage >> Edge(label="re-normalize", color="#d97706", style="bold", fontcolor="#b45309") >> norm

        # --- shared LLM model: every agent calls Gemini ---
        for n in (teams, linker, verif, vmaw, adj, router):
            n >> Edge(color="#6366f1", style="dotted") >> gemini
        triage >> Edge(color="#6366f1", style="dotted", label="route?") >> adj
        # --- tool calls during extraction / verification ---
        teams >> Edge(color="#0d9488", style="dotted", label="tool calls") >> hgnc
        teams >> Edge(color="#0d9488", style="dotted") >> hgvs
        verif >> Edge(color="#0d9488", style="dotted") >> negn

        # --- persistence + observability ---
        teams >> Edge(style="dotted") >> ckpt
        router >> art; router >> bq
        router >> Edge(style="dotted", label="OpenTelemetry") >> logs
        router >> Edge(style="dotted") >> mon

        # --- output / human ---
        art >> ui; bq >> ui; ui >> sme

    shutil.copyfile(base + ".png", OUT / "system_design.png")
    print("wrote", (OUT / "system_design.png").relative_to(OUT.parents[2]))


def main() -> int:
    overall(); preprocess(); planner(); agentic_loop(); linker()
    verifiers(); pingback_loop(); vmaw(); decision()
    try:
        system_design()
    except Exception as exc:  # noqa: BLE001
        print(f"system_design skipped ({exc}); install the 'diagrams' package to enable it")
    print("all diagrams written to", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

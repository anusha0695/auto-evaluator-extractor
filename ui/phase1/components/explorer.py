"""
explorer — builds the interactive block-centric PDF explorer HTML (U6-U10).

Consumes the assembled BlockViews + rendered page images and emits a single
self-contained HTML string for `st.components.v1.html`. All interaction
(click-to-inspect, role/umbrella toggle, page switch) is handled in-page JS,
so there is no per-click Streamlit rerun.

Multi-umbrella blocks use a SPLIT FILL in umbrella mode (option 2): the box is
divided into N diagonal bands, one per umbrella. Role mode is always a solid
fill (a block has exactly one text_role).
"""

from __future__ import annotations

import html
import json
from typing import Any

from ui.phase1.block_view import BlockView

# Color ramps: [light fill, strong stroke, dark text]. Mirrors the design
# system ramps so it reads natively in the host.
_RAMP = {
    "blue":   ["#E6F1FB", "#185FA5", "#0C447C"],
    "teal":   ["#E1F5EE", "#0F6E56", "#085041"],
    "coral":  ["#FAECE7", "#993C1D", "#712B13"],
    "purple": ["#EEEDFE", "#534AB7", "#3C3489"],
    "amber":  ["#FAEEDA", "#854F0B", "#633806"],
    "pink":   ["#FBEAF0", "#993556", "#72243E"],
    "green":  ["#EAF3DE", "#3B6D11", "#27500A"],
    "gray":   ["#F1EFE8", "#5F5E5A", "#444441"],
    "red":    ["#FCEBEB", "#A32D2D", "#791F1F"],
}

_ROLE_COLOR = {
    "fax_transport_noise": "gray", "report_title": "blue",
    "vendor_branding": "blue", "practice_block": "teal",
    "patient_demographics": "pink", "specimen_metadata": "amber",
    "ordering_provider": "coral", "accession_block": "amber",
    "panel_or_test_name": "purple", "methodology": "teal",
    "results_table": "red", "interpretation": "green",
    "clinical_significance": "green", "references": "gray",
    "cpt_codes": "gray", "electronic_signature": "gray",
    "page_header": "gray", "page_footer": "gray",
    "chart_image_caption": "gray", "disclaimer_or_notes": "gray",
    "other": "gray",
}

_UMB_COLOR = {
    "report_metadata": "blue",                       # cool, pure blue
    "Genomic_Variant_umbrella": "red",               # warm, red
    "tested_biomarker_umbrella": "amber",            # warm, orange-yellow (was purple — too close to blue)
    "other_molecular_biomarker_umbrella": "teal",    # cool, blue-green (was green)
    "none": "gray",
}


def _view_to_payload(v: BlockView) -> dict[str, Any]:
    return {
        "block_id": v.block_id,
        "page": v.page_number,
        "bbox": v.bbox,
        "role": v.text_role,
        "umbrellas": v.target_umbrella_hints,
        "confidence": v.confidence,
        "rationale": v.rationale,
        "text": v.text,
        "entities": [
            {"text": e.text, "umbrella": e.umbrella,
             "models": e.source_models} for e in v.entities
        ],
        "fields": [
            {"field": f.field_name, "value": _stringify(f.value),
             "type": f.prov_type} for f in v.fields
        ],
    }


def _stringify(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def build_explorer_html(
    views: list[BlockView],
    pages: list[dict],
    *,
    height: int = 760,
) -> str:
    """Return the full HTML string for the explorer component."""
    payload = {
        "blocks": [_view_to_payload(v) for v in views],
        "pages": pages,
        "ramp": _RAMP,
        "roleColor": _ROLE_COLOR,
        "umbColor": _UMB_COLOR,
    }
    data_json = json.dumps(payload)
    # html-escape only the closing-script guard; JSON is safe inside a script tag
    # except for "</script>" sequences, which JSON.dumps won't produce.
    return _TEMPLATE.replace("__DATA__", data_json).replace("__HEIGHT__", str(height))


# The template is plain HTML/CSS/JS. __DATA__ is replaced with the JSON payload.
_TEMPLATE = r"""
<div id="bcx-root" style="font-family: ui-sans-serif, system-ui, sans-serif; color:#1a1a1a;">
  <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:10px;">
    <div style="display:flex; gap:6px; align-items:center;">
      <span style="font-size:13px; color:#6b6b6b;">Color by</span>
      <button id="bcx-role" class="bcx-btn">Block role</button>
      <button id="bcx-umb" class="bcx-btn">Umbrella</button>
    </div>
    <div id="bcx-pages" style="display:flex; gap:6px; align-items:center; margin-left:auto;">
      <span style="font-size:13px; color:#6b6b6b;">Page</span>
    </div>
  </div>

  <div id="bcx-grid" style="display:grid; grid-template-columns: 1fr 1fr; gap:12px; align-items:start;">
    <div style="background:#f6f5f2; border-radius:12px; padding:10px;">
      <div style="display:flex; align-items:center; gap:6px; margin-bottom:8px; flex-wrap:wrap;">
        <button id="bcx-zoom-out" class="bcx-btn" aria-label="zoom out">−</button>
        <span id="bcx-zoom-label" style="font-size:12px; color:#6b6b6b; min-width:42px; text-align:center;">100%</span>
        <button id="bcx-zoom-in" class="bcx-btn" aria-label="zoom in">+</button>
        <button id="bcx-zoom-fit" class="bcx-btn">Fit</button>
        <span style="margin-left:auto; font-size:12px; color:#6b6b6b;">Page width</span>
        <input type="range" id="bcx-col" min="30" max="72" value="50" style="width:110px;" />
      </div>
      <div id="bcx-viewport" style="position:relative; overflow:auto; max-height:760px; background:#fff; border:0.5px solid rgba(0,0,0,0.15); border-radius:8px;">
        <div id="bcx-stage" style="position:relative; transform-origin: top left; width:max-content;">
          <div id="bcx-canvas" style="position:relative;"></div>
        </div>
      </div>
      <div id="bcx-legend" style="display:flex; flex-wrap:wrap; gap:6px 12px; margin-top:8px; font-size:11px; color:#6b6b6b;"></div>
    </div>
    <div id="bcx-inspector" style="background:#fff; border:0.5px solid rgba(0,0,0,0.15); border-radius:12px; padding:1rem 1.25rem; min-height:380px;"></div>
  </div>
</div>

<style>
  .bcx-btn { font-size:13px; padding:4px 10px; border-radius:8px; border:0.5px solid rgba(0,0,0,0.3); background:transparent; cursor:pointer; }
  .bcx-btn.active { background:#eceae3; }
  #bcx-root table { border-collapse:collapse; width:100%; }
  #bcx-viewport { cursor:grab; }
  #bcx-viewport.bcx-panning { cursor:grabbing; }
</style>

<script>
(function(){
  const DATA = __DATA__;
  const RAMP = DATA.ramp, roleColor = DATA.roleColor, umbColor = DATA.umbColor;
  const blocksByPage = {};
  DATA.blocks.forEach(b => { (blocksByPage[b.page] = blocksByPage[b.page] || []).push(b); });
  const pageMeta = {}; DATA.pages.forEach(p => pageMeta[p.page_number] = p);
  const pageNums = [...new Set(DATA.blocks.map(b=>b.page))].sort((a,b)=>a-b);

  let mode = "role";
  let page = pageNums[0] || 1;
  let selected = null;
  let zoom = 1;

  const canvas = document.getElementById("bcx-canvas");
  const stage = document.getElementById("bcx-stage");
  const viewport = document.getElementById("bcx-viewport");
  const inspector = document.getElementById("bcx-inspector");
  const legend = document.getElementById("bcx-legend");
  const pagesBar = document.getElementById("bcx-pages");
  const grid = document.getElementById("bcx-grid");

  pageNums.forEach(n => {
    const btn = document.createElement("button");
    btn.className = "bcx-btn"; btn.textContent = n; btn.dataset.page = n;
    btn.onclick = () => { page = n; selected = null; render(); };
    pagesBar.appendChild(btn);
  });

  function realUmbrellas(b){ return (b.umbrellas||[]).filter(u => u !== "none"); }

  function blockFill(b){
    if (mode === "role"){
      const c = RAMP[roleColor[b.role] || "gray"];
      return { css: c[0], stroke: c[1], text: c[2] };
    }
    const us = realUmbrellas(b);
    if (us.length === 0){ const c = RAMP["gray"]; return {css:c[0], stroke:c[1], text:c[2]}; }
    if (us.length === 1){ const c = RAMP[umbColor[us[0]]||"gray"]; return {css:c[0], stroke:c[1], text:c[2]}; }
    // SPLIT FILL: diagonal bands, one per umbrella (option 2)
    const stops = []; const n = us.length;
    us.forEach((u,i) => {
      const c = RAMP[umbColor[u]||"gray"][0];
      const a = Math.round(100*i/n), b2 = Math.round(100*(i+1)/n);
      stops.push(`${c} ${a}% ${b2}%`);
    });
    const c0 = RAMP[umbColor[us[0]]||"gray"];
    return { css: `linear-gradient(135deg, ${stops.join(", ")})`, stroke: c0[1], text: c0[2] };
  }

  function applyZoom(){
    stage.style.transform = `scale(${zoom})`;
    document.getElementById("bcx-zoom-label").textContent = Math.round(zoom*100) + "%";
  }

  function render(){
    const meta = pageMeta[page];
    canvas.innerHTML = "";
    // Base (unzoomed) canvas width = current viewport content width, so the
    // page fits the column at 100%; zoom scales the stage above that.
    const baseW = Math.max(200, viewport.clientWidth - 2);
    const aspect = (meta && meta.width && meta.height) ? (meta.width / meta.height) : 0.77;
    canvas.style.width = baseW + "px";
    canvas.style.height = Math.round(baseW / aspect) + "px";
    canvas.style.background = "#fff";
    canvas.style.overflow = "hidden";
    if (meta && meta.data_uri){
      const img = document.createElement("img");
      img.src = meta.data_uri;
      img.style.cssText = "position:absolute; inset:0; width:100%; height:100%; display:block;";
      img.draggable = false;
      canvas.appendChild(img);
    }
    (blocksByPage[page]||[]).forEach(b => {
      if (!b.bbox || b.bbox.length < 4) return;
      const [x0,y0,x1,y1] = b.bbox;
      const f = blockFill(b);
      const sel = b.block_id === selected;
      const el = document.createElement("div");
      el.style.cssText =
        `position:absolute; left:${x0*100}%; top:${y0*100}%; width:${(x1-x0)*100}%; height:${(y1-y0)*100}%;`+
        `background:${f.css}; border:${sel?"2px":"1px"} solid ${f.stroke}; border-radius:3px; cursor:pointer;`+
        `opacity:${sel?0.92:0.5}; box-sizing:border-box;`;
      el.title = `${b.block_id} · ${b.role}`;
      el.onclick = (ev) => { ev.stopPropagation(); selected = b.block_id; render(); };
      canvas.appendChild(el);
    });
    applyZoom();
    renderLegend(); renderInspector();
    document.getElementById("bcx-role").classList.toggle("active", mode==="role");
    document.getElementById("bcx-umb").classList.toggle("active", mode==="umb");
    [...pagesBar.querySelectorAll("button")].forEach(btn =>
      btn.classList.toggle("active", Number(btn.dataset.page)===page));
  }

  function renderLegend(){
    const present = new Set();
    (blocksByPage[page]||[]).forEach(b => {
      if (mode==="role") present.add(b.role);
      else realUmbrellas(b).forEach(u=>present.add(u)) || present.add("none");
    });
    const map = mode==="role" ? roleColor : umbColor;
    legend.innerHTML = "";
    [...present].forEach(k => {
      const c = RAMP[map[k]||"gray"];
      const item = document.createElement("span");
      item.style.cssText = "display:inline-flex; align-items:center; gap:4px;";
      item.innerHTML = `<span style="width:10px;height:10px;border-radius:2px;background:${c[0]};border:1px solid ${c[1]};"></span>${k}`;
      legend.appendChild(item);
    });
    if (mode==="umb"){
      const note = document.createElement("span");
      note.style.cssText = "color:#999;";
      note.textContent = "split box = serves multiple teams";
      legend.appendChild(note);
    }
  }

  function esc(s){ return (s==null?"":String(s)).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

  function chip(text, ramp){
    const c = RAMP[ramp]||RAMP.gray;
    return `<span style="display:inline-block; font-size:11px; padding:2px 8px; border-radius:8px; background:${c[0]}; color:${c[2]}; margin:0 4px 4px 0;">${esc(text)}</span>`;
  }

  function renderInspector(){
    const b = (blocksByPage[page]||[]).find(x=>x.block_id===selected);
    if (!b){ inspector.innerHTML = `<p style="color:#6b6b6b; font-size:14px;">Click a block on the page to inspect it.</p>`; return; }
    const rc = RAMP[roleColor[b.role]||"gray"];
    const umbChips = (b.umbrellas||[]).map(u => chip(u, umbColor[u]||"gray")).join("");
    const ents = (b.entities||[]).length
      ? b.entities.map(e=>`<tr><td style="padding:3px 8px 3px 0; font-family:ui-monospace,monospace; font-size:12px;">${esc(e.text)}</td>`+
          `<td style="padding:3px 0; font-size:12px; color:#6b6b6b;">${esc(e.umbrella)} · ${esc((e.models||[]).join(", "))}</td></tr>`).join("")
      : `<tr><td style="font-size:12px; color:#999;">— none (or dropped) —</td></tr>`;
    const tagRamp = t => t==="absent"?"gray": t==="derived"?"amber": t==="inferred"?"purple":"teal";
    const flds = (b.fields||[]).length
      ? b.fields.map(f=>`<tr><td style="padding:3px 8px 3px 0; font-size:12px;">${esc(f.field)}</td>`+
          `<td style="padding:3px 8px 3px 0; font-family:ui-monospace,monospace; font-size:12px; color:#185FA5;">${esc(f.value)}</td>`+
          `<td style="padding:3px 0;">${chip(f.type, tagRamp(f.type))}</td></tr>`).join("")
      : `<tr><td style="font-size:12px; color:#999;">— no fields sourced here —</td></tr>`;
    const conf = (b.confidence==null) ? "" : ` · conf ${Number(b.confidence).toFixed(2)}`;

    inspector.innerHTML =
      `<div style="display:flex; align-items:center; gap:8px; margin-bottom:10px;">
         <span style="font-size:11px; font-family:ui-monospace,monospace; padding:2px 8px; border-radius:8px; background:${rc[0]}; color:${rc[2]};">block ${esc(b.block_id)}</span>
         <span style="font-size:15px; font-weight:500;">${esc(b.role)}</span>
         <span style="font-size:12px; color:#999; margin-left:auto;">page ${b.page}${conf}</span>
       </div>
       <div style="margin-bottom:12px;">
         <div style="font-size:12px; color:#6b6b6b; margin-bottom:4px;">target umbrella hints</div>${umbChips}
       </div>
       <div style="margin-bottom:12px;">
         <div style="font-size:12px; color:#6b6b6b; margin-bottom:4px;">block text</div>
         <div style="font-size:12px; line-height:1.5; background:#f6f5f2; padding:8px 10px; border-radius:8px; max-height:120px; overflow:auto;">${esc(b.text)||"<span style='color:#999'>(no text — table/image block)</span>"}</div>
       </div>
       <div style="margin-bottom:12px;">
         <div style="font-size:12px; color:#6b6b6b; margin-bottom:4px;">entities found here <span style="color:#999;">(parser_hypothesis · occurrences)</span></div>
         <table>${ents}</table>
       </div>
       <div>
         <div style="font-size:12px; color:#6b6b6b; margin-bottom:4px;">fields sourced from this block <span style="color:#999;">(provenance)</span></div>
         <table>${flds}</table>
       </div>`;
  }

  document.getElementById("bcx-role").onclick = () => { mode="role"; render(); };
  document.getElementById("bcx-umb").onclick = () => { mode="umb"; render(); };

  // ----- Zoom controls -----
  function setZoom(z){
    zoom = Math.min(5, Math.max(0.5, Math.round(z*20)/20));  // clamp 0.5–5x, 5% steps
    applyZoom();
  }
  document.getElementById("bcx-zoom-in").onclick  = () => setZoom(zoom + 0.25);
  document.getElementById("bcx-zoom-out").onclick = () => setZoom(zoom - 0.25);
  document.getElementById("bcx-zoom-fit").onclick = () => { setZoom(1); viewport.scrollTo(0,0); };

  // Ctrl/Cmd + wheel = zoom toward cursor; plain wheel = normal scroll.
  viewport.addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const cx = viewport.scrollLeft + (e.clientX - rect.left);
    const cy = viewport.scrollTop  + (e.clientY - rect.top);
    const prev = zoom;
    setZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9));
    const ratio = zoom / prev;
    viewport.scrollLeft = cx * ratio - (e.clientX - rect.left);
    viewport.scrollTop  = cy * ratio - (e.clientY - rect.top);
  }, { passive: false });

  // ----- Drag to pan (only meaningful when zoomed/scrollable) -----
  let panning = false, startX = 0, startY = 0, startSL = 0, startST = 0, moved = false;
  viewport.addEventListener("mousedown", (e) => {
    panning = true; moved = false;
    startX = e.clientX; startY = e.clientY;
    startSL = viewport.scrollLeft; startST = viewport.scrollTop;
    viewport.classList.add("bcx-panning");
  });
  window.addEventListener("mousemove", (e) => {
    if (!panning) return;
    const dx = e.clientX - startX, dy = e.clientY - startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
    viewport.scrollLeft = startSL - dx;
    viewport.scrollTop  = startST - dy;
  });
  window.addEventListener("mouseup", () => {
    panning = false; viewport.classList.remove("bcx-panning");
  });
  // Suppress a block click that was actually the end of a pan-drag.
  canvas.addEventListener("click", (e) => { if (moved) e.stopPropagation(); }, true);

  // ----- Page-width slider (resizes the left column) -----
  const colSlider = document.getElementById("bcx-col");
  function applyCol(){
    const left = Number(colSlider.value);
    grid.style.gridTemplateColumns = `${left}fr ${100-left}fr`;
    render();  // re-measure viewport width so the page refits the new column
  }
  colSlider.addEventListener("input", applyCol);

  // Re-measure when the iframe finishes layout / is resized, so the page
  // image fits the actual column width (clientWidth can be 0 on first paint).
  window.addEventListener("resize", render);
  render();
  requestAnimationFrame(() => requestAnimationFrame(render));
})();
</script>
"""

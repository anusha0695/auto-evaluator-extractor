"""
entity_explorer — interactive, Block-explorer-style canvas for ALL entities (P3-M8).

One self-contained HTML/JS string (no per-click Streamlit rerun). Layout:
  • a Review / Accepted filter + Technical / Binding toggles
  • left: the entity list (filtered)
  • top-right: the real page; selecting an entity highlights it (yellow marker)
    and its linked entities (blue marker) — text reads through (mix-blend multiply)
  • a draggable divider
  • bottom-right: the agent trace, collapsible, with each step's reasoning (↳ why);
    binding-verifier steps are shown only when the Binding toggle is on.

Read-only by design — Approve / Edit / Keep stay in the Review queue tab. All
geometry + traces are precomputed in Python (`build_entity_payload`) so the JS
only draws.
"""

from __future__ import annotations

import json
from typing import Any

# Light translucent washes (NO multiply blend — multiply turns near-black on dark
# scanned faxes). The selected entity also gets a solid outline so it's always
# identifiable even over a dark/overlapping region.
ENTITY_MARK = "rgba(255,205,0,0.30)"      # selected — soft amber wash + outline
LINK_MARK = "rgba(56,132,255,0.18)"       # linked — faint blue wash
ENTITY_OUTLINE = "rgba(214,138,0,0.95)"   # selected outline


def build_entity_explorer_html(entities: list[dict[str, Any]], pages: list[dict],
                               *, height: int = 820) -> str:
    payload = {"entities": entities, "pages": pages,
               "entityMark": ENTITY_MARK, "linkMark": LINK_MARK, "entityOutline": ENTITY_OUTLINE}
    return (_TEMPLATE
            .replace("__DATA__", json.dumps(payload))
            .replace("__HEIGHT__", str(height)))


_TEMPLATE = r"""
<div id="ee-root" style="font-family: ui-sans-serif, system-ui, sans-serif; color:#1a1a1a;">
  <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:10px;">
    <div style="display:inline-flex; border:0.5px solid rgba(0,0,0,0.3); border-radius:8px; overflow:hidden;">
      <button id="ee-f-review" class="ee-seg">Review</button>
      <button id="ee-f-accepted" class="ee-seg">Accepted</button>
    </div>
    <label style="font-size:12px; color:#6b6b6b; display:flex; align-items:center; gap:4px;">
      <input type="checkbox" id="ee-tech"/> Technical</label>
    <label style="font-size:12px; color:#6b6b6b; display:flex; align-items:center; gap:4px;">
      <input type="checkbox" id="ee-bind"/> Binding checks</label>
    <span id="ee-count" style="margin-left:auto; font-size:12px; color:#6b6b6b;"></span>
  </div>

  <div style="display:grid; grid-template-columns: 180px 1fr; gap:12px; align-items:start;">
    <div id="ee-list" style="background:#f6f5f2; border-radius:12px; padding:8px; max-height:__HEIGHT__px; overflow:auto;"></div>

    <div>
      <div id="ee-page-wrap" style="background:#f6f5f2; border-radius:12px; padding:10px;">
        <div id="ee-pagemeta" style="font-size:12px; color:#6b6b6b; margin-bottom:6px;"></div>
        <div id="ee-viewport" style="position:relative; overflow:auto; height:420px; background:#fff; border:0.5px solid rgba(0,0,0,0.15); border-radius:8px;">
          <div id="ee-canvas" style="position:relative;"></div>
        </div>
        <div style="font-size:11px; color:#6b6b6b; margin-top:6px;">
          <span style="background:rgba(255,205,0,0.30); outline:2px solid rgba(214,138,0,0.95); outline-offset:-1px; padding:0 6px;">selected entity</span>
          <span style="background:rgba(56,132,255,0.18); padding:0 6px; margin-left:8px;">linked entity</span>
        </div>
      </div>

      <div id="ee-divider" title="drag to resize" style="display:flex; align-items:center; justify-content:center; gap:6px; padding:6px 0; cursor:row-resize; color:#9a9a9a; font-size:11px;">
        ⋯ drag to resize ⋯
      </div>

      <div id="ee-trace-wrap" style="background:#fff; border:0.5px solid rgba(0,0,0,0.15); border-radius:12px; padding:10px 12px;">
        <div id="ee-trace-head" style="display:flex; align-items:center; justify-content:space-between; cursor:pointer;">
          <div id="ee-trace-title" style="font-size:13px; font-weight:500;">Agent trace</div>
          <span id="ee-trace-chev" style="font-size:13px; color:#6b6b6b;">▾</span>
        </div>
        <div id="ee-trace-body" style="margin-top:8px;"></div>
      </div>
    </div>
  </div>
</div>

<style>
  .ee-seg { font-size:12px; padding:5px 14px; border:none; background:#fff; color:#6b6b6b; cursor:pointer; }
  .ee-seg.active { background:#E6F1FB; color:#0C447C; }
  .ee-row { font-size:12px; padding:6px 8px; border-radius:6px; cursor:pointer; color:#444; }
  .ee-row.active { background:#E6F1FB; color:#0C447C; font-weight:500; }
  .ee-row.linked { box-shadow: inset 2px 0 0 #185fa5; }
  .ee-row .ee-badge { color:#A32D2D; }
</style>

<script>
(function(){
  const DATA = __DATA__;
  const ents = DATA.entities || [];
  const pageMeta = {}; (DATA.pages||[]).forEach(p => pageMeta[p.page_number] = p);
  let filter = "review", sel = null, technical = false, binding = false, open = true;

  const listEl = document.getElementById("ee-list");
  const canvas = document.getElementById("ee-canvas");
  const viewport = document.getElementById("ee-viewport");
  const traceBody = document.getElementById("ee-trace-body");
  const traceTitle = document.getElementById("ee-trace-title");
  const pagemeta = document.getElementById("ee-pagemeta");
  const countEl = document.getElementById("ee-count");

  function esc(s){ return (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
  function filtered(){ return ents.filter(e => (filter==="review") ? e.status==="review" : e.status!=="review"); }

  function renderList(){
    const items = filtered();
    countEl.textContent = items.length + " " + filter + " · " + ents.length + " total";
    listEl.innerHTML = "";
    if (!items.length){ listEl.innerHTML = `<div style="font-size:12px;color:#999;padding:8px;">No ${filter} entities.</div>`; return; }
    const selEnt = ents.find(x => x.ref===sel);
    const linkedSet = new Set((selEnt && selEnt.linked_refs) || []);
    items.forEach(e => {
      const row = document.createElement("div");
      const isLinked = linkedSet.has(e.ref);
      row.className = "ee-row" + (e.ref===sel ? " active" : "") + (isLinked ? " linked" : "");
      row.innerHTML = (e.status==="review" ? `<span class="ee-badge">⚑ </span>` : "") +
                      (isLinked ? `<span style="color:#185fa5">🔗 </span>` : "") +
                      `${esc(e.label||e.ref)}<div style="font-size:10px;color:#999;">${esc(e.section||"")}</div>`;
      row.onclick = () => { sel = e.ref; open = true; renderAll(); };
      listEl.appendChild(row);
    });
    document.getElementById("ee-f-review").classList.toggle("active", filter==="review");
    document.getElementById("ee-f-accepted").classList.toggle("active", filter==="accepted");
  }

  function box(b, fill, selected){
    const [x0,y0,x1,y1] = b;
    // light translucent wash (NO multiply — that goes near-black on dark scans);
    // the selected entity also gets a solid outline so it's always identifiable.
    const outline = selected ? `outline:2px solid ${DATA.entityOutline};outline-offset:-1px;` : "";
    return `<div style="position:absolute;left:${x0*100}%;top:${y0*100}%;width:${(x1-x0)*100}%;height:${(y1-y0)*100}%;`+
           `background:${fill};border-radius:1px;${outline}"></div>`;
  }

  function renderPage(){
    const e = ents.find(x => x.ref===sel);
    canvas.innerHTML = "";
    if (!e){ pagemeta.textContent = "Select an entity to view its page."; return; }
    const meta = pageMeta[e.page];
    const linkNote = (e.linked_labels && e.linked_labels.length) ? "  ·  🔗 linked: " + e.linked_labels.join(", ") : "";
    pagemeta.textContent = "page " + e.page + " · " + (e.label||"") + (e.status==="review" ? " · ⚑ in review queue" : "") + linkNote;
    const w = Math.max(200, viewport.clientWidth - 2);
    const aspect = (meta && meta.width && meta.height) ? meta.width/meta.height : 0.77;
    canvas.style.width = w + "px"; canvas.style.height = Math.round(w/aspect) + "px";
    canvas.style.position = "relative";
    let inner = "";
    if (meta && meta.data_uri) inner += `<img src="${meta.data_uri}" style="position:absolute;inset:0;width:100%;height:100%;display:block;"/>`;
    (e.linked_boxes||[]).forEach(b => inner += box(b, DATA.linkMark, false));
    (e.boxes||[]).forEach(b => inner += box(b, DATA.entityMark, true));   // selected drawn last + outlined
    canvas.innerHTML = inner;
  }

  function renderTrace(){
    const e = ents.find(x => x.ref===sel);
    traceTitle.textContent = e ? ("Agent trace · " + (e.label||e.ref)) : "Agent trace";
    document.getElementById("ee-trace-chev").textContent = open ? "▾" : "▸";
    if (!open){ traceBody.style.display = "none"; return; }
    traceBody.style.display = "block";
    if (!e){ traceBody.innerHTML = `<div style="font-size:12px;color:#999;">Select an entity to see how it was processed.</div>`; return; }
    const steps = (e.trace||[]).filter(s => binding || s.kind!=="binding");
    const icon = {extraction:"📄",linking:"🔗",verification:"🔎",repair:"🔧",resolution:"🤝"};
    const hiddenB = (e.trace||[]).filter(s => s.kind==="binding").length;
    let html = "";
    if (hiddenB && !binding) html += `<div style="font-size:11px;color:#999;margin-bottom:6px;">${hiddenB} binding check(s) hidden — tick “Binding checks”.</div>`;
    if (!steps.length) html += `<div style="font-size:12px;color:#999;">No recorded steps for this field.</div>`;
    html += `<div style="border-left:2px solid rgba(0,0,0,0.18); padding-left:10px; display:flex; flex-direction:column; gap:10px;">`;
    steps.forEach(s => {
      const line = technical ? s.technical : s.plain;
      html += `<div><div style="font-size:12px;">${icon[s.phase]||"•"} <b>${esc(s.phase)}</b> · ${esc(s.node)}</div>`+
              `<div style="font-size:11px;color:#555;">${esc(line)}</div>`;
      if (s.reasoning) {
        const lbl = (s.reasoning_scope === "section")
          ? "note (section-level — not specific to this field)" : "why (this field)";
        html += `<div style="font-size:11px;color:#888;font-style:italic;">↳ ${lbl}: ${esc(s.reasoning)}</div>`;
      }
      html += `</div>`;
    });
    html += `</div>`;
    traceBody.innerHTML = html;
  }

  function renderAll(){ renderList(); renderPage(); renderTrace(); }

  document.getElementById("ee-f-review").onclick = () => { filter="review"; sel=null; renderAll(); };
  document.getElementById("ee-f-accepted").onclick = () => { filter="accepted"; sel=null; renderAll(); };
  document.getElementById("ee-tech").onchange = (ev) => { technical = ev.target.checked; renderTrace(); };
  document.getElementById("ee-bind").onchange = (ev) => { binding = ev.target.checked; renderTrace(); };
  document.getElementById("ee-trace-head").onclick = () => { open = !open; renderTrace(); };

  // draggable divider resizes the page viewport height
  let dragging = false, startY = 0, startH = 0;
  const divider = document.getElementById("ee-divider");
  divider.addEventListener("mousedown", (e) => { dragging = true; startY = e.clientY; startH = viewport.clientHeight; e.preventDefault(); });
  window.addEventListener("mousemove", (e) => { if (!dragging) return; const h = Math.max(160, startH + (e.clientY - startY)); viewport.style.height = h + "px"; });
  window.addEventListener("mouseup", () => { if (dragging){ dragging = false; renderPage(); } });

  window.addEventListener("resize", renderPage);
  renderAll();
  requestAnimationFrame(() => requestAnimationFrame(renderPage));
})();
</script>
"""

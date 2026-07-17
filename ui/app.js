/**
 * Main Application Controller — SME Review Portal (Multi-Document)
 */
(function () {
  // Documents are discovered at runtime via GET /api/docs.
  // The backend scans local_runs/artifacts/<doc_id>/ folders that contain
  // extraction_v2.json. To add a new doc to the portal, just add a folder
  // there — no code edits needed.
  let DOCS = [];

  let currentDocIdx = 0;
  let extraction = null, blocks = [], agentTraceData = [], escalationData = null;
  let verificationData = null, repairLog = [];
  let currentSection = 'report_metadata', selectedField = null;
  let fieldDecisions = {};
  let recordDecisions = {};  // keyed by server field_id like 'Genomic_Variants[3]' or 'Genomic_Variants[__new_0__]'
  let checkedFields = new Set();
  let allDocsData = [];

  async function loadJSON(dir, file) {
    const r = await fetch(dir + file);
    return r.json();
  }

  // ── Review Capture (SME Review Portal, DR-4 endpoints) ─────────────────
  //
  // The client uses its own field key format `section::field[idx]` because
  // the existing code (bulk-select, table rendering) is built on it. The
  // server uses dot-notation IDs per FR-1 (`Genomic_Variants[3].aa_change`).
  // Two translation helpers below sit at the network boundary.

  const SECTION_TO_LIST_KEY = {
    // Maps existing UI section names → the inner list key used by the server.
    // Same 3 umbrella-wrapper sections + singular report_metadata.
    'report_metadata': null,  // singular — no list
    'Genomic_Variant_umbrella': 'Genomic_Variants',
    'other_molecular_biomarker_umbrella': 'other_molecular_biomarkers',
    'tested_biomarker_umbrella': 'tested_biomarkers',
  };
  const LIST_KEY_TO_SECTION = Object.fromEntries(
    Object.entries(SECTION_TO_LIST_KEY).filter(([_, v]) => v).map(([k, v]) => [v, k])
  );

  function fkToServerFieldId(fk) {
    // Convert client-side fk → server-side field_id (dot notation).
    // Examples:
    //   report_metadata::patient_name → report_metadata.patient_name
    //   Genomic_Variant_umbrella::aa_change[3] → Genomic_Variants[3].aa_change
    if (typeof fk !== 'string' || !fk.includes('::')) return fk;
    const [section, fieldPart] = fk.split('::', 2);
    if (section === 'report_metadata') {
      return `report_metadata.${fieldPart}`;
    }
    const listKey = SECTION_TO_LIST_KEY[section];
    if (!listKey) return `${section}.${fieldPart}`;   // unknown section — best-effort
    const idxMatch = fieldPart.match(/^(.+)\[(\d+)\]$/);
    if (idxMatch) {
      return `${listKey}[${idxMatch[2]}].${idxMatch[1]}`;
    }
    return `${listKey}.${fieldPart}`;   // no index in fieldPart — unusual
  }

  function serverFieldIdToFk(sid) {
    // Inverse: dot notation → client fk. Used when hydrating state from the server.
    if (typeof sid !== 'string') return sid;
    if (sid.startsWith('report_metadata.')) {
      return 'report_metadata::' + sid.substring('report_metadata.'.length);
    }
    const m = sid.match(/^(\w+)\[(\d+)\]\.(.+)$/);
    if (m) {
      const [_, listKey, idx, fieldName] = m;
      const section = LIST_KEY_TO_SECTION[listKey];
      if (section) return `${section}::${fieldName}[${idx}]`;
    }
    // Record-only ID like "Genomic_Variants[7]" (reject_record target) — leave as-is
    return sid;
  }

  function currentDocId() {
    return DOCS[currentDocIdx] ? DOCS[currentDocIdx].id : null;
  }

  // ── Save-indicator UX (FR-3.3) ─────────────────────────────────────────
  // Small "✓ Saved" appears for ~1s next to the last action. A separate
  // persistent error banner appears (and stays) if a save fails.

  let _saveIndicatorTimeout = null;

  function showSaveIndicator() {
    const el = document.getElementById('saveIndicator');
    if (!el) return;
    el.textContent = '✓ Saved';
    el.className = 'save-indicator visible';
    if (_saveIndicatorTimeout) clearTimeout(_saveIndicatorTimeout);
    _saveIndicatorTimeout = setTimeout(() => {
      el.className = 'save-indicator';
    }, 1200);
  }

  function showSaveError(msg) {
    const el = document.getElementById('saveErrorBanner');
    if (!el) return;
    el.textContent = '⚠ ' + msg + ' — your local changes are unsaved.';
    el.className = 'save-error-banner visible';
  }

  function clearSaveError() {
    const el = document.getElementById('saveErrorBanner');
    if (el) el.className = 'save-error-banner';
  }

  // ── Server-side save (POST /api/review/<doc>/action) ──────────────────

  async function saveActionToServer(fk, action, opts = {}) {
    // opts: { originalValue, correctedValue, naturalKey, fieldIdOverride }
    const docId = currentDocId();
    if (!docId) return null;
    const serverFieldId = opts.fieldIdOverride || fkToServerFieldId(fk);
    const body = {
      field_id: serverFieldId,
      action: action,
      original_value: opts.originalValue !== undefined ? opts.originalValue : null,
      corrected_value: opts.correctedValue !== undefined ? opts.correctedValue : null,
      natural_key: opts.naturalKey || null,
    };
    try {
      const r = await fetch(`/api/review/${encodeURIComponent(docId)}/action`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const j = await r.json();
      clearSaveError();
      showSaveIndicator();
      return j;
    } catch (e) {
      console.error('saveActionToServer failed', e);
      showSaveError(`Save failed (${e.message || e})`);
      throw e;
    }
  }

  // ── Load prior review state on doc open (FR-3.4) ───────────────────────

  async function hydrateReviewStateFromServer(docId) {
    // Populate fieldDecisions + recordDecisions from server-persisted actions
    // so refresh / reopen restores the SME's prior work.
    fieldDecisions = {};
    recordDecisions = {};
    let submissionStatus = 'not_started';
    let stateBundle = null;
    try {
      const r = await fetch(`/api/review/${encodeURIComponent(docId)}/state`);
      if (!r.ok) return { submissionStatus, stateBundle };
      stateBundle = await r.json();
      const state = stateBundle.state;
      submissionStatus = stateBundle.progress ? stateBundle.progress.submission_status : 'not_started';

      // Rebuild both decision maps from LATEST un-superseded action per field_id.
      const latestByFieldId = {};
      for (const a of state.actions || []) {
        if (a.superseded_at) continue;
        latestByFieldId[a.field_id] = a;
      }
      for (const [serverFieldId, a] of Object.entries(latestByFieldId)) {
        if (a.action === 'reject_record') {
          // Record-level — keep in recordDecisions, keyed by server field_id.
          recordDecisions[serverFieldId] = { status: 'rejected', serverActionId: a.action_id };
        } else if (a.action === 'add_missing') {
          recordDecisions[serverFieldId] = {
            status: 'added',
            values: a.corrected_value || {},
            serverActionId: a.action_id,
          };
        } else {
          const fk = serverFieldIdToFk(serverFieldId);
          if (a.action === 'accept' || a.action === 'implicit_accept') {
            fieldDecisions[fk] = { status: 'accepted', serverActionId: a.action_id };
          } else if (a.action === 'correct') {
            fieldDecisions[fk] = { status: 'corrected', correctedValue: a.corrected_value, serverActionId: a.action_id };
          } else if (a.action === 'reject') {
            fieldDecisions[fk] = { status: 'rejected', serverActionId: a.action_id };
          }
        }
      }
    } catch (e) {
      console.warn('Could not hydrate review state', e);
    }
    return { submissionStatus, stateBundle };
  }

  // ── Record-level actions (reject_record + add_missing per FR-2, FR-4.3) ─
  //
  // Server field_id format for record-scoped actions:
  //   reject_record  → "Genomic_Variants[3]"   (list name + index, no .field)
  //   add_missing    → "Genomic_Variants[__new_0__]"  (special __new_N__ marker)
  //
  // Client-side these live in `recordDecisions` (separate from fieldDecisions
  // to avoid conflating record and field concerns in bulk-select logic etc.).

  function sectionToServerListKey(section) {
    return SECTION_TO_LIST_KEY[section] || null;
  }

  async function rejectRecord(section, index, event) {
    if (event) event.stopPropagation();
    const listKey = sectionToServerListKey(section);
    if (!listKey) return;
    const serverFieldId = `${listKey}[${index}]`;
    const prior = recordDecisions[serverFieldId];
    recordDecisions[serverFieldId] = { status: 'rejected' };  // optimistic
    // Re-render the section that this record belongs to
    rerenderCurrentSection();
    try {
      // Uses saveActionToServer with the fieldIdOverride escape hatch —
      // no fk-to-serverFieldId translation needed for record-level.
      await saveActionToServer(null, 'reject_record', { fieldIdOverride: serverFieldId });
    } catch (e) {
      if (prior) recordDecisions[serverFieldId] = prior; else delete recordDecisions[serverFieldId];
      rerenderCurrentSection();
    }
  }

  async function undoRejectRecord(section, index) {
    // Locate the action_id in the state and DELETE it via the supersede endpoint.
    // Simple approach: refetch state, find matching un-superseded action, DELETE.
    const listKey = sectionToServerListKey(section);
    if (!listKey) return;
    const serverFieldId = `${listKey}[${index}]`;
    const docId = currentDocId();
    if (!docId) return;
    try {
      const r = await fetch(`/api/review/${encodeURIComponent(docId)}/state`);
      const stateBundle = await r.json();
      const target = (stateBundle.state.actions || []).find(
        a => a.field_id === serverFieldId && a.action === 'reject_record' && !a.superseded_at
      );
      if (!target) return;
      await fetch(`/api/review/${encodeURIComponent(docId)}/action/${target.action_id}`, {
        method: 'DELETE',
      });
      delete recordDecisions[serverFieldId];
      showSaveIndicator();
      rerenderCurrentSection();
    } catch (e) {
      showSaveError(`Undo failed (${e.message || e})`);
    }
  }

  // ── Report missing record — dynamic form based on section schema ─────
  //
  // Opens a small inline form whose fields are inferred from the first
  // existing record's keys (skipping provenance/metadata). SME fills in
  // whatever they know; empty fields are omitted from the record.

  function openMissingRecordForm(section) {
    const listKey = sectionToServerListKey(section);
    if (!listKey) return;
    const records = extraction[section] && extraction[section][listKey];
    if (!records || !records.length) {
      alert(
        'No existing record available in this section to use as a template. ' +
        'Add at least one record via the pipeline first.'
      );
      return;
    }
    // Infer field schema from the first existing record — exclude metadata keys
    const templateFields = Object.keys(records[0]).filter(
      k => k !== 'provenance' && k !== 'llm_confidence_score'
           && k !== 'needs_review' && k !== 'review_reason'
           && k !== 'hgvs_normalized' && !k.startsWith('count_of_')
    );
    const modal = document.getElementById('missingRecordModal');
    if (!modal) return;
    const inputsHtml = templateFields.map(f => `
      <div class="mr-form-row">
        <label>${escHtml(f)}</label>
        <input type="text" data-field="${escAttr(f)}" class="mr-input" placeholder="(leave blank if unknown)">
      </div>
    `).join('');
    modal.innerHTML = `
      <div class="mr-scrim" onclick="window.app.cancelMissingRecordForm()"></div>
      <div class="mr-dialog" role="dialog" aria-labelledby="mrTitle">
        <h3 id="mrTitle">+ Report missing record: ${escHtml(section.replace('_umbrella', '').replace(/_/g, ' '))}</h3>
        <p style="font-size:.82rem;color:var(--g500);margin:0 0 12px 0">
          Fill in the fields you know — the extractor missed this record.
          Leave blank fields empty.
        </p>
        <form id="mrForm" onsubmit="return window.app.submitMissingRecordForm('${escAttr(section)}', event)">
          ${inputsHtml}
          <div class="mr-actions">
            <button type="button" class="btn" onclick="window.app.cancelMissingRecordForm()">Cancel</button>
            <button type="submit" class="btn btn-accept">Save missing record</button>
          </div>
        </form>
      </div>
    `;
    modal.className = 'missing-record-modal visible';
    // Focus first input
    setTimeout(() => { const inp = modal.querySelector('.mr-input'); if (inp) inp.focus(); }, 30);
  }

  function cancelMissingRecordForm() {
    const modal = document.getElementById('missingRecordModal');
    if (modal) { modal.className = 'missing-record-modal'; modal.innerHTML = ''; }
  }

  async function submitMissingRecordForm(section, event) {
    if (event) event.preventDefault();
    const listKey = sectionToServerListKey(section);
    if (!listKey) return false;
    const modal = document.getElementById('missingRecordModal');
    if (!modal) return false;
    // Collect non-empty inputs into a new record dict
    const record = {};
    modal.querySelectorAll('.mr-input').forEach(inp => {
      const val = (inp.value || '').trim();
      if (val) record[inp.dataset.field] = val;
    });
    if (!Object.keys(record).length) {
      alert('Please fill in at least one field.');
      return false;
    }
    // Determine a unique __new_N__ marker
    const existingNew = Object.keys(recordDecisions).filter(k => k.startsWith(`${listKey}[__new_`));
    const newIdx = existingNew.length;
    const serverFieldId = `${listKey}[__new_${newIdx}__]`;
    recordDecisions[serverFieldId] = { status: 'added', values: record };
    cancelMissingRecordForm();
    rerenderCurrentSection();
    try {
      await saveActionToServer(null, 'add_missing', {
        fieldIdOverride: serverFieldId,
        correctedValue: record,
      });
    } catch (e) {
      delete recordDecisions[serverFieldId];
      rerenderCurrentSection();
    }
    return false;
  }

  // ── Real reject (sets field value to null in ground_truth per FR-4.3) ─
  async function rejectFieldValue(fk, event) {
    if (event) event.stopPropagation();
    const prior = fieldDecisions[fk];
    fieldDecisions[fk] = { status: 'rejected' };   // optimistic
    renderExtractionTable();
    renderFieldDetail(null);
    try {
      await saveActionToServer(fk, 'reject');
    } catch (e) {
      // Rollback on failure
      if (prior) fieldDecisions[fk] = prior; else delete fieldDecisions[fk];
      renderExtractionTable();
    }
  }


  async function fetchDocList() {
    try {
      const r = await fetch('/api/docs');
      const j = await r.json();
      return Array.isArray(j.docs) ? j.docs : [];
    } catch (e) {
      console.error('Failed to fetch /api/docs', e);
      return [];
    }
  }

  async function init() {
    DOCS = await fetchDocList();
    if (DOCS.length === 0) {
      const list = document.getElementById('docList');
      if (list) list.innerHTML = '<li style="padding:12px;color:var(--muted)">No documents found in local_runs/artifacts/. Run the pipeline to populate.</li>';
      return;
    }

    // Load all documents for aggregate stats and dashboard verifications
    for (const doc of DOCS) {
      try {
        const [esc, trace, ver] = await Promise.all([
          loadJSON(doc.dir, 'escalation_queue_banded.json'),
          loadJSON(doc.dir, 'agent_trace.json'),
          loadJSON(doc.dir, 'verification_v2.json').catch(() => null)
        ]);
        const lastStep = trace[trace.length - 1];
        allDocsData.push({
          id: doc.id,
          label: doc.label,
          dir: doc.dir,
          escalation: esc,
          verdict: lastStep ? lastStep.verdict : 'unknown',
          verification: ver
        });
      } catch (e) { console.warn('Could not load stats for', doc.id, e); }
    }

    renderDocList();
    renderAggregateStats();
    setupResizeHandles();   // Fix (user feedback): drag to resize panel widths
    showDashboard();
  }

  // ── Draggable column dividers (fix: resize each tab width) ────────────
  //
  // Two 6px handles between sidebar / center / right panel let SME drag to
  // adjust widths. Widths are tracked in px on documentView.style.gridTemplateColumns
  // so resize survives re-renders. Min widths prevent panels from collapsing.
  function setupResizeHandles() {
    const view = document.getElementById('documentView');
    if (!view || view._resizeHandlesReady) return;
    view._resizeHandlesReady = true;

    const configs = [
      { id: 'resizeHandleLeft',  col: 0, min: 200, max: 600 },   // sidebar
      { id: 'resizeHandleRight', col: 4, min: 300, max: 1200 },  // right panel
    ];
    configs.forEach(cfg => {
      const handle = document.getElementById(cfg.id);
      if (!handle) return;
      handle.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const startX = e.clientX;
        const style = getComputedStyle(view);
        const cols = style.gridTemplateColumns.split(' ').map(v => parseFloat(v));
        // cols = [sidebar, handle, center, handle, right]
        const startVal = cols[cfg.col];
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        function onMove(ev) {
          const dx = ev.clientX - startX;
          const newCols = [...cols];
          if (cfg.col === 0) {
            // Left handle drag: sidebar grows/shrinks; center absorbs the change
            newCols[0] = Math.max(cfg.min, Math.min(cfg.max, startVal + dx));
            newCols[2] = Math.max(300, cols[2] - (newCols[0] - startVal));
          } else if (cfg.col === 4) {
            // Right handle drag: right panel is on the other side, so subtract dx
            newCols[4] = Math.max(cfg.min, Math.min(cfg.max, startVal - dx));
            newCols[2] = Math.max(300, cols[2] + (startVal - newCols[4]));
          }
          view.style.gridTemplateColumns = newCols.map(v => `${Math.round(v)}px`).join(' ');
        }
        function onUp() {
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          document.body.style.cursor = '';
          document.body.style.userSelect = '';
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    });
  }

  async function loadDocument(idx) {
    currentDocIdx = idx;
    const doc = DOCS[idx];
    fieldDecisions = {};
    checkedFields.clear();
    selectedField = null;
    currentSection = 'report_metadata';

    // Show detailed document view and hide dashboard view
    document.getElementById('dashboardView').style.display = 'none';
    document.getElementById('documentView').style.display = 'grid';

    // Show header back button
    const backBtn = document.getElementById('backToQueueBtn');
    if (backBtn) backBtn.style.display = 'block';

    try {
      [extraction, blocks, agentTraceData, escalationData, verificationData, repairLog] = await Promise.all([
        loadJSON(doc.dir, 'extraction_v2.json'),
        loadJSON(doc.dir, 'blocks.json').catch(() => []),
        loadJSON(doc.dir, 'agent_trace.json'),
        loadJSON(doc.dir, 'escalation_queue_banded.json'),
        loadJSON(doc.dir, 'verification_v2.json'),
        loadJSON(doc.dir, 'repair_log.json')
      ]);
    } catch (e) { console.error('Doc load error', e); }

    // FR-3.4 / FR-5.1 — hydrate fieldDecisions from server-persisted actions
    // so refresh restores prior review work. Sets fieldDecisions + recordDecisions.
    await hydrateReviewStateFromServer(doc.id);
    clearSaveError();
    // Q12 A / Stage 4.2 — render the submitted-banner if this doc has been submitted.
    renderSubmittedBanner();

    document.querySelectorAll('.section-tab').forEach(t => t.classList.remove('active'));
    document.querySelector('[data-section="report_metadata"]').classList.add('active');

    renderTabCounts();
    renderExtractionTable();
    renderVerifiers();
    renderEscalations();
    setupTabListeners();

    window.pdfViewer.init(doc.dir);
    window.agentTrace.render(agentTraceData, document.getElementById('traceTimeline'));

    // Update verdict display
    const lastStep = agentTraceData[agentTraceData.length - 1];
    const verdict = lastStep ? lastStep.verdict : 'unknown';
    const verdictEl = document.getElementById('verdictText');
    if (verdictEl) verdictEl.textContent = verdict;

    document.querySelectorAll('.doc-item').forEach((el, i) => el.classList.toggle('active', i === idx));
    renderFieldDetail(null);
  }

  function showDashboard() {
    // Hide document view, show dashboard view
    document.getElementById('documentView').style.display = 'none';
    document.getElementById('dashboardView').style.display = 'block';
    
    // Hide header back button
    const backBtn = document.getElementById('backToQueueBtn');
    if (backBtn) backBtn.style.display = 'none';

    // Reset filter dropdown to "Show All"
    const filterSelect = document.getElementById('queueFilter');
    if (filterSelect) filterSelect.value = 'all';

    // Clear active state in doc list
    document.querySelectorAll('.doc-item').forEach(el => el.classList.remove('active'));

    // Populate stats
    let totalDocs = DOCS.length;
    let autoAccepted = 0;
    let escalated = 0;
    let pending = 0;
    allDocsData.forEach(d => {
      if (d.verdict === 'auto_accept') autoAccepted++;
      else if (['sme_flag', 'partial_accept', 'fixable', 'escalate'].includes(d.verdict)) escalated++;
      else pending++;
    });
    
    const totalEl = document.getElementById('dashStatTotal');
    const acceptedEl = document.getElementById('dashStatAccepted');
    const escalatedEl = document.getElementById('dashStatEscalated');
    const pendingEl = document.getElementById('dashStatPending');
    
    if (totalEl) totalEl.textContent = totalDocs;
    if (acceptedEl) acceptedEl.textContent = autoAccepted;
    if (escalatedEl) escalatedEl.textContent = escalated;
    if (pendingEl) pendingEl.textContent = pending;

    // Populate table (Stage 4.3 — extended with SME review columns)
    const tbody = document.getElementById('dashboardTableBody');
    if (tbody) {
      tbody.innerHTML = '';
      allDocsData.forEach((d, i) => {
        const tr = document.createElement('tr');
        tr.dataset.docId = d.id;

        // Count total escalations across all bands
        let escCount = 0;
        if (d.escalation && d.escalation.bands) {
          Object.keys(d.escalation.bands).forEach(band => {
            escCount += (d.escalation.bands[band] || []).length;
          });
        }

        // Verdict styling
        let verdictClass = 'verdict-unknown';
        let isClickable = true;
        if (d.verdict === 'auto_accept') {
          verdictClass = 'verdict-accepted';
        } else if (['sme_flag', 'partial_accept', 'fixable', 'escalate'].includes(d.verdict)) {
          verdictClass = 'verdict-escalated';
        } else {
          verdictClass = 'verdict-pending';
          isClickable = d.verdict !== 'pending';
        }

        // Review columns populated later by populateReviewMetricsIntoDashboard —
        // rendered as em-dash placeholders now so table shape stays stable.
        tr.innerHTML = `
          <td><strong>${escHtml(d.id)}</strong></td>
          <td><span class="verdict-badge ${verdictClass}">${escHtml(d.verdict)}</span></td>
          <td><span class="escalation-badge-count ${escCount > 0 ? 'active' : 'zero'}">${escCount} escalation(s)</span></td>
          <td class="review-status-cell" data-review-col="status"><span class="sme-status-badge not-started">—</span></td>
          <td class="review-metric-cell" data-review-col="accuracy" style="text-align:right">—</td>
          <td class="review-metric-cell" data-review-col="precision" style="text-align:right">—</td>
          <td class="review-metric-cell" data-review-col="recall" style="text-align:right">—</td>
          <td class="review-fields-cell" data-review-col="fields" style="text-align:right">—</td>
          <td style="text-align: center;">
            <button class="btn btn-accept" style="padding:4px 8px; font-size:0.75rem; ${!isClickable ? 'opacity:0.5; cursor:not-allowed;' : ''}" ${!isClickable ? 'disabled' : ''} onclick="window.app.loadDocument(${i})">
              ${d.verdict === 'pending' ? 'Pending' : 'Review →'}
            </button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    // Stage 4.3 — populate the aggregate review metric cards + per-doc columns
    populateReviewMetricsIntoDashboard();
  }

  // ── Landing-page review metrics (Q11 B, FR-7) ─────────────────────────
  //
  // Fetches /api/review/metrics and populates the second stat row + the
  // per-doc review columns (Status / Acc / Prec / Rec / Fields). Called from
  // showDashboard() after the base table is rendered.

  async function populateReviewMetricsIntoDashboard() {
    try {
      const r = await fetch('/api/review/metrics');
      if (!r.ok) return;
      const data = await r.json();

      // Aggregate cards (top of dashboard)
      const agg = data.aggregate || {};
      const setCard = (id, fmt) => {
        const el = document.getElementById(id);
        if (el) el.textContent = fmt;
      };
      const pct = v => (typeof v === 'number' && v > 0) ? Math.round(v * 100) + '%' : '—';
      setCard('reviewStatAccuracy', pct(agg.accuracy));
      setCard('reviewStatPrecision', pct(agg.precision));
      setCard('reviewStatRecall', pct(agg.recall));
      setCard('reviewStatProgress',
        `${agg.fields_reviewed || 0} / ${agg.fields_total || 0}`);

      // Per-doc rows — index by doc_id
      const perDoc = {};
      (data.per_doc || []).forEach(p => { perDoc[p.doc_id] = p; });

      document.querySelectorAll('#dashboardTableBody tr').forEach(tr => {
        const docId = tr.dataset.docId;
        if (!docId || !perDoc[docId]) return;
        const info = perDoc[docId];
        const status = info.submission_status || 'not_started';
        const statusCell = tr.querySelector('[data-review-col="status"]');
        if (statusCell) {
          const label = status === 'submitted' ? 'submitted'
                       : status === 'draft'    ? 'draft'
                                                : 'not started';
          statusCell.innerHTML = `<span class="sme-status-badge ${status}">${label}</span>`;
        }
        // Metrics only meaningful for submitted docs
        const metrics = info.metrics;
        const cellAcc  = tr.querySelector('[data-review-col="accuracy"]');
        const cellPrec = tr.querySelector('[data-review-col="precision"]');
        const cellRec  = tr.querySelector('[data-review-col="recall"]');
        const cellFlds = tr.querySelector('[data-review-col="fields"]');
        if (metrics) {
          if (cellAcc)  cellAcc.textContent  = pct(metrics.accuracy);
          if (cellPrec) cellPrec.textContent = pct(metrics.precision);
          if (cellRec)  cellRec.textContent  = pct(metrics.recall);
        }
        if (cellFlds) {
          cellFlds.textContent = `${info.fields_reviewed || 0} / ${info.fields_total || 0}`;
        }

        // Replace Review → button with 📤 Submit for drafts (quick-submit)
        if (status === 'draft') {
          const actionCell = tr.lastElementChild;
          if (actionCell) {
            actionCell.innerHTML = `
              <button class="btn btn-accept" style="padding:4px 8px; font-size:0.72rem"
                      onclick="window.app.quickSubmitDoc('${escAttr(docId)}')">
                📤 Submit
              </button>
              <button class="btn" style="padding:4px 8px; font-size:0.72rem;background:var(--g100);color:var(--g600)"
                      onclick="event.stopPropagation();window.app.loadDocumentById('${escAttr(docId)}')">
                Open
              </button>
            `;
          }
        }
      });
    } catch (e) {
      console.warn('Could not populate review metrics into dashboard', e);
    }
  }

  function loadDocumentById(docId) {
    const idx = DOCS.findIndex(d => d.id === docId);
    if (idx >= 0) loadDocument(idx);
  }

  async function quickSubmitDoc(docId) {
    // Load the doc first (so reviewer name + banner work), then trigger the submit flow.
    loadDocumentById(docId);
    // Wait a tick for the DOM to update
    setTimeout(() => submitDocument(), 300);
  }

  function filterQueue(val) {
    const rows = document.querySelectorAll('#dashboardTableBody tr');
    rows.forEach((row, idx) => {
      const d = allDocsData[idx];
      if (!d) return;
      
      let show = false;
      if (val === 'all') {
        show = true;
      } else if (val === 'review') {
        show = ['sme_flag', 'partial_accept', 'fixable', 'escalate'].includes(d.verdict);
      } else if (val === 'accepted') {
        show = d.verdict === 'auto_accept';
      } else if (val === 'pending') {
        show = !['auto_accept', 'sme_flag', 'partial_accept', 'fixable', 'escalate'].includes(d.verdict);
      }
      
      row.style.display = show ? '' : 'none';
    });
  }

  function renderDocList() {
    const list = document.getElementById('docList');
    if (!list) return;
    list.innerHTML = '';
    DOCS.forEach((doc, i) => {
      const li = document.createElement('li');
      li.className = 'doc-item' + (i === 0 ? ' active' : '');
      const d = allDocsData[i];
      const isEscalated = d && d.verdict === 'escalate';
      li.innerHTML = `
        <svg class="doc-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M4 1h5.586L13 4.414V14a1 1 0 01-1 1H4a1 1 0 01-1-1V2a1 1 0 011-1zm5 0v4h4"/></svg>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(doc.label)}</span>
        ${isEscalated ? '<span style="width:8px;height:8px;border-radius:50%;background:var(--cardinal-red);flex-shrink:0" title="Escalated"></span>' : '<span style="width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0" title="Auto Accepted"></span>'}
      `;
      li.addEventListener('click', () => loadDocument(i));
      list.appendChild(li);
    });
  }

  function renderAggregateStats() {
    let autoAccepted = 0;
    let aggBands = { judgment: 0, unresolved: 0, review_light: 0, drop_audit: 0 };
    allDocsData.forEach(d => {
      if (d.verdict === 'auto_accept') autoAccepted++;
      if (d.escalation && d.escalation.bands) {
        Object.keys(aggBands).forEach(band => { aggBands[band] += (d.escalation.bands[band] || []).length; });
      }
    });
    
    const statTotalEl = document.getElementById('statTotal');
    const statAcceptedEl = document.getElementById('statAccepted');
    const bandJudgmentEl = document.getElementById('bandJudgment');
    const bandUnresolvedEl = document.getElementById('bandUnresolved');
    const bandReviewLightEl = document.getElementById('bandReviewLight');
    const bandDropAuditEl = document.getElementById('bandDropAudit');
    
    if (statTotalEl) statTotalEl.textContent = DOCS.length;
    if (statAcceptedEl) statAcceptedEl.textContent = autoAccepted;
    if (bandJudgmentEl) bandJudgmentEl.textContent = aggBands.judgment;
    if (bandUnresolvedEl) bandUnresolvedEl.textContent = aggBands.unresolved;
    if (bandReviewLightEl) bandReviewLightEl.textContent = aggBands.review_light;
    if (bandDropAuditEl) bandDropAuditEl.textContent = aggBands.drop_audit;
  }

  // Fields to hide from the report_metadata table
  const HIDDEN_META_FIELDS = ['provenance', 'llm_confidence_score', 'Signing_Pathologist', 'Ordering_Provider_Phone', 'Additional_Provider_Name'];

  function renderTabCounts() {
    if (!extraction) return;
    const meta = extraction.report_metadata;
    document.getElementById('countMeta').textContent = meta ? Object.keys(meta).filter(k => !HIDDEN_META_FIELDS.includes(k)).length : 0;
    const gv = extraction.Genomic_Variant_umbrella;
    document.getElementById('countVariant').textContent = gv ? gv.count_of_Genomic_Variants || 0 : 0;
    const mb = extraction.other_molecular_biomarker_umbrella;
    document.getElementById('countBiomarker').textContent = mb ? mb.count_of_other_molecular_biomarkers || 0 : 0;
    const tb = extraction.tested_biomarker_umbrella;
    document.getElementById('countTested').textContent = tb ? tb.count_of_tested_biomarkers || 0 : 0;
  }

  function setupTabListeners() {
    document.querySelectorAll('.section-tab').forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll('.section-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentSection = tab.dataset.section;
        selectedField = null;
        checkedFields.clear();
        renderExtractionTable();
        renderFieldDetail(null);
        updateBulkActionBar();
      };
    });
  }

  function renderExtractionTable() {
    const area = document.querySelector('.extraction-area');
    if (currentSection === 'report_metadata') {
      // Ensure table is visible
      let tbl = area.querySelector('.extraction-table');
      if (!tbl) { restoreTableView(area); tbl = area.querySelector('.extraction-table'); }
      if (tbl) tbl.style.display = '';
      const tbody = document.getElementById('extractionBody');
      if (tbody) { tbody.innerHTML = ''; renderMetadataRows(tbody); }
      updateSelectAllCheckboxState();
      // Remove any custom view
      const custom = area.querySelector('.custom-section-view');
      if (custom) custom.remove();
      // Add confidence header before the table
      renderSectionConfidenceHeader(area, '📋', 'REPORT METADATA', extraction.report_metadata);
    } else {
      // Hide table, show custom view
      let tbl = area.querySelector('.extraction-table');
      if (tbl) tbl.style.display = 'none';
      const existingHeader = area.querySelector('.section-header');
      if (existingHeader) existingHeader.remove();
      let custom = area.querySelector('.custom-section-view');
      if (!custom) { custom = document.createElement('div'); custom.className = 'custom-section-view'; custom.style.overflowY = 'auto'; custom.style.flex = '1'; area.appendChild(custom); }
      custom.innerHTML = '';
      if (currentSection === 'Genomic_Variant_umbrella') renderVariantView(custom);
      else if (currentSection === 'other_molecular_biomarker_umbrella') renderBiomarkerView(custom);
      else if (currentSection === 'tested_biomarker_umbrella') renderTestedView(custom);
    }
  }

  function restoreTableView(area) {
    let tbl = area.querySelector('.extraction-table');
    if (tbl) { tbl.style.display = ''; return; }
    // Recreate table if it was removed
    const table = document.createElement('table');
    table.className = 'extraction-table';
    table.innerHTML = '<thead><tr><th style="width:30px"></th><th>Extraction Field</th><th>Extracted Value</th><th>Source</th><th style="width:100px">SME Action</th></tr></thead><tbody id="extractionBody"></tbody>';
    area.appendChild(table);
  }

  function fieldKey(section, field) { return section + '::' + field; }

  function renderFieldActions(fk) {
    const dec = fieldDecisions[fk];
    // For reviewed fields, show the status badge + a small ↺ undo button so
    // the SME can re-edit (fix: user could not change decisions after re-open).
    const undoBtn = `<button class="btn-field-undo" title="Undo decision — clear this field's review" onclick="window.app.undoFieldDecision('${escAttr(fk)}', event)">↺</button>`;
    if (dec && dec.status === 'accepted') return `<span class="field-status accepted">✓ Accepted</span>${undoBtn}`;
    if (dec && dec.status === 'corrected') return `<span class="field-status corrected" title="Corrected to: ${escHtml(dec.correctedValue)}">✎ Corrected</span>${undoBtn}`;
    if (dec && dec.status === 'rejected') return `<span class="field-status rejected" title="Field marked as wrongly extracted; value → null in ground truth">✕ Rejected</span>${undoBtn}`;
    return `<button class="btn-field-accept" title="Accept" onclick="window.app.acceptField('${escAttr(fk)}', event)">✓</button>`
         + `<button class="btn-field-reject" title="Correct value" onclick="window.app.rejectField('${escAttr(fk)}', event)">✎</button>`
         + `<button class="btn-field-null" title="Reject: mark as wrongly extracted" onclick="window.app.rejectFieldValue('${escAttr(fk)}', event)">✕</button>`;
  }

  // ── Undo a prior field decision (fix: re-open editing) ────────────────
  //
  // Called from the ↺ button next to any accepted/corrected/rejected badge.
  // Clears the local decision AND DELETEs the server-side action so it's
  // marked as superseded. Buttons re-appear so the SME can pick a new action.
  async function undoFieldDecision(fk, event) {
    if (event) event.stopPropagation();
    const prior = fieldDecisions[fk];
    if (!prior) return;
    const priorActionId = prior.serverActionId;
    delete fieldDecisions[fk];   // optimistic
    renderExtractionTable();
    rerenderCurrentSection();
    if (!priorActionId) return;  // no server-side ID (freshly saved before hydration)
    try {
      const docId = currentDocId();
      const r = await fetch(
        `/api/review/${encodeURIComponent(docId)}/action/${encodeURIComponent(priorActionId)}`,
        { method: 'DELETE' }
      );
      if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
      showSaveIndicator();
    } catch (e) {
      // Rollback the local state if server rejects
      fieldDecisions[fk] = prior;
      renderExtractionTable();
      rerenderCurrentSection();
      showSaveError(`Undo failed (${e.message || e})`);
    }
  }

  function buildRow(section, field, val, prov, tbody) {
    const fk = fieldKey(section, field);
    const dec = fieldDecisions[fk];
    const tr = document.createElement('tr');
    if (dec && dec.status === 'accepted') tr.classList.add('row-accepted');
    if (dec && dec.status === 'corrected') tr.classList.add('row-corrected');
    if (dec && dec.status === 'rejected') tr.classList.add('row-rejected');
    const isReviewed = dec && (dec.status === 'accepted' || dec.status === 'corrected' || dec.status === 'rejected');
    const displayVal = dec && dec.status === 'corrected' ? dec.correctedValue : (val !== null ? String(val) : null);
    tr.innerHTML = `
      <td><input type="checkbox" class="field-checkbox" ${isReviewed ? 'disabled style="opacity:0.3"' : ''} ${checkedFields.has(fk) ? 'checked' : ''} onchange="window.app.handleFieldCheck('${escAttr(fk)}', this)"></td>
      <td class="field-name">${escHtml(field)}</td>
      <td class="field-value ${displayVal === null ? 'null-val' : ''}">${displayVal !== null ? escHtml(displayVal).substring(0, 120) : '—'}</td>
      <td><span class="prov-badge ${prov.type || 'absent'}">${prov.type || 'absent'}</span></td>
      <td class="action-cell">${renderFieldActions(fk)}</td>
    `;
    tr.addEventListener('click', (e) => {
      if (e.target.type === 'checkbox' || e.target.tagName === 'BUTTON' || e.target.tagName === 'INPUT') return;
      selectRow(tr, field, prov, val);
    });
    tbody.appendChild(tr);
  }

  function renderMetadataRows(tbody) {
    const meta = extraction.report_metadata;
    if (!meta) return;
    const provMap = {};
    (meta.provenance || []).forEach(p => { provMap[p.field_name] = p; });
    Object.keys(meta).filter(k => !HIDDEN_META_FIELDS.includes(k)).forEach(field => {
      buildRow('report_metadata', field, meta[field], provMap[field] || {}, tbody);
    });
  }

  function renderSectionConfidenceHeader(area, icon, title, sectionData) {
    // Remove existing header if any
    const existing = area.querySelector('.section-header');
    if (existing) existing.remove();
    if (!sectionData || sectionData.llm_confidence_score == null) return;
    const conf = sectionData.llm_confidence_score;
    const confPct = Math.round(conf * 100);
    const confClass = confPct >= 90 ? 'high' : confPct >= 70 ? 'medium' : 'low';
    const confColor = confPct >= 90 ? 'var(--green)' : confPct >= 70 ? 'var(--amber)' : 'var(--cardinal-red)';
    const header = document.createElement('div');
    header.className = 'section-header';
    header.innerHTML = `
      <div><h2>${icon} ${title}</h2></div>
      <div class="confidence-meter"><div class="conf-label">LLM Confidence</div><div class="conf-value ${confClass}">${confPct}%</div><div class="confidence-bar"><div class="confidence-bar-fill" style="width:${confPct}%;background:${confColor}"></div></div></div>
    `;
    area.insertBefore(header, area.firstChild);
  }

  // === GENOMIC VARIANTS — Summary table + expandable detail ===
  let activeVariantIdx = -1;

  function renderVariantView(container) {
    const section = extraction.Genomic_Variant_umbrella;
    if (!section || !section.Genomic_Variants) { container.innerHTML = '<div style="padding:40px;text-align:center;color:var(--g400)">No genomic variants extracted</div>'; return; }
    const conf = section.llm_confidence_score || 0;
    const confPct = Math.round(conf * 100);
    const confClass = confPct >= 90 ? 'high' : confPct >= 70 ? 'medium' : 'low';
    const confColor = confPct >= 90 ? 'var(--green)' : confPct >= 70 ? 'var(--amber)' : 'var(--cardinal-red)';

    container.innerHTML = `
      <div class="section-header">
        <div><h2>🧬 GENOMIC VARIANTS</h2><div class="section-subtitle">${section.Genomic_Variants.length} variant(s) · Select a row, then select a field to review →</div></div>
        <div class="confidence-meter"><div class="conf-label">LLM Confidence</div><div class="conf-value ${confClass}">${confPct}%</div><div class="confidence-bar"><div class="confidence-bar-fill" style="width:${confPct}%;background:${confColor}"></div></div></div>
      </div>
      <div style="padding:8px 12px 0"><button class="btn-report-missing" onclick="window.app.openMissingRecordForm('Genomic_Variant_umbrella')" title="Report a variant the extractor missed">+ Report missing variant</button></div>
      <div style="padding:0 12px"><table class="variant-summary" id="variantSummaryTable">
        <thead><tr><th style="width:30px"></th><th>Gene</th><th>Result</th><th>Coding Change</th><th>AA Change</th><th>Significance</th><th style="width:60px"></th><th style="width:40px"></th></tr></thead>
        <tbody id="variantSummaryBody"></tbody>
      </table></div>
      <div id="variantDetailArea"></div>
    `;

    const tbody = document.getElementById('variantSummaryBody');
    section.Genomic_Variants.forEach((v, vi) => {
      const isDetected = v.result && v.result.toLowerCase() !== 'not detected';
      const tr = document.createElement('tr');
      if (v.needs_review) tr.classList.add('needs-review');
      if (vi === activeVariantIdx) tr.classList.add('active-variant');

      // Record-level rejection tint (Stage 4.1b)
      const recFieldId = `Genomic_Variants[${vi}]`;
      const recDec = recordDecisions[recFieldId];
      if (recDec && recDec.status === 'rejected') tr.classList.add('record-rejected');

      const fields = Object.keys(v).filter(k => k !== 'provenance' && k !== 'hgvs_normalized' && k !== 'needs_review' && k !== 'review_reason');
      const unreviewedFields = fields.filter(field => {
        const fk = fieldKey('Genomic_Variant_umbrella', field + '[' + vi + ']');
        const dec = fieldDecisions[fk];
        return !(dec && (dec.status === 'accepted' || dec.status === 'corrected'));
      });
      const allChecked = unreviewedFields.length > 0 && unreviewedFields.every(field => {
        const fk = fieldKey('Genomic_Variant_umbrella', field + '[' + vi + ']');
        return checkedFields.has(fk);
      });
      const isReviewed = unreviewedFields.length === 0;

      const isRejected = recDec && recDec.status === 'rejected';
      const rejectBtn = isRejected
        ? `<button class="btn-record-undo" onclick="event.stopPropagation();window.app.undoRejectRecord('Genomic_Variant_umbrella',${vi})" title="Undo record rejection">↺</button>`
        : `<button class="btn-record-reject" onclick="event.stopPropagation();window.app.rejectRecord('Genomic_Variant_umbrella',${vi},event)" title="Reject this whole record (row removed from ground truth)">✕</button>`;

      tr.innerHTML = `
        <td><input type="checkbox" class="field-checkbox" ${isReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.handleVariantCheck(${vi}, this)"></td>
        <td class="gene-cell">${escHtml(v.gene_studied || '—')}</td>
        <td class="result-cell ${isDetected ? 'result-detected' : 'result-not-detected'}">${escHtml(v.result || '—')}</td>
        <td class="change-cell">${escHtml(v.coding_dna_change || '—')}</td>
        <td class="change-cell">${escHtml(v.amino_acid_change || '—')}</td>
        <td class="sig-cell" title="${escHtml(v.clinical_significance || '')}">${escHtml((v.clinical_significance || '—').substring(0, 50))}${(v.clinical_significance||'').length > 50 ? '…' : ''}</td>
        <td>${v.needs_review ? '<span class="review-flag">⚠ Review</span>' : ''}${isRejected ? '<span class="record-rejected-badge">REJECTED</span>' : ''}</td>
        <td>${rejectBtn}</td>
      `;
      tr.addEventListener('click', (e) => {
        if (e.target.type === 'checkbox') return;
        activeVariantIdx = vi === activeVariantIdx ? -1 : vi;
        renderVariantView(container);
      });
      tbody.appendChild(tr);
    });

    // Render expanded detail if a variant is selected
    if (activeVariantIdx >= 0 && activeVariantIdx < section.Genomic_Variants.length) {
      renderVariantDetail(section.Genomic_Variants[activeVariantIdx], activeVariantIdx);
    }
  }

  function renderVariantDetail(variant, vi) {
    const area = document.getElementById('variantDetailArea');
    const provMap = {};
    (variant.provenance || []).forEach(p => { provMap[p.field_name] = p; });
    const fields = Object.keys(variant).filter(k => k !== 'provenance' && k !== 'hgvs_normalized' && k !== 'needs_review' && k !== 'review_reason');

    const unreviewedFields = fields.filter(field => {
      const fk = fieldKey('Genomic_Variant_umbrella', field + '[' + vi + ']');
      const dec = fieldDecisions[fk];
      return !(dec && (dec.status === 'accepted' || dec.status === 'corrected'));
    });
    const allChecked = unreviewedFields.length > 0 && unreviewedFields.every(field => {
      const fk = fieldKey('Genomic_Variant_umbrella', field + '[' + vi + ']');
      return checkedFields.has(fk);
    });
    const isAllReviewed = unreviewedFields.length === 0;

    let html = `<div class="variant-detail-panel">
      <h3><span class="detail-icon">✎</span> Field-level Review (Variant #${vi + 1}: ${escHtml(variant.gene_studied || '')})</h3>
      ${variant.needs_review ? `<div style="padding:8px 12px;background:var(--amber-bg);border-radius:var(--r-sm);margin-bottom:12px;font-size:.78rem;color:#92400E;font-weight:600">⚠ ${escHtml(variant.review_reason || 'This variant needs SME review')}</div>` : ''}
      <table class="variant-fields-table">
        <thead>
          <tr>
            <th style="width:30px;padding:6px 12px;text-align:left"><input type="checkbox" class="field-checkbox" ${isAllReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.toggleSelectAll(this)"></th>
            <th style="padding:6px 12px;text-align:left;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Field</th>
            <th style="padding:6px 12px;text-align:left;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Value</th>
            <th style="width:100px;padding:6px 12px;text-align:center;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Action</th>
          </tr>
        </thead>
        <tbody>`;

    fields.forEach(field => {
      const val = variant[field];
      const fk = fieldKey('Genomic_Variant_umbrella', field + '[' + vi + ']');
      const dec = fieldDecisions[fk];
      const isFieldReviewed = dec && (dec.status === 'accepted' || dec.status === 'corrected');
      const displayVal = dec && dec.status === 'corrected' ? dec.correctedValue : (val !== null ? String(val) : null);
      html += `<tr${dec ? (dec.status === 'accepted' ? ' class="row-accepted"' : ' class="row-corrected"') : ''}>
        <td><input type="checkbox" class="field-checkbox" ${isFieldReviewed ? 'disabled style="opacity:0.3"' : ''} ${checkedFields.has(fk) ? 'checked' : ''} onchange="window.app.handleFieldCheck('${escAttr(fk)}', this)"></td>
        <td class="vf-name">${escHtml(field)}</td>
        <td class="vf-value ${displayVal === null ? 'null' : ''}">${displayVal !== null ? escHtml(displayVal).substring(0,150) : '—'}</td>
        <td class="vf-actions">${renderFieldActions(fk)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    area.innerHTML = html;

    // Wire up click on field rows for provenance highlight
    area.querySelectorAll('.variant-fields-table tbody tr').forEach((tr, fi) => {
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', (e) => {
        if (e.target.tagName === 'BUTTON' || e.target.type === 'checkbox') return;
        const field = fields[fi];
        const prov = provMap[field] || {};
        selectedField = { field, prov, value: variant[field] };
        renderFieldDetail(selectedField);
        if (DOCS[currentDocIdx].hasPdf && prov.block_id) {
          // If block_id uses 'block_N' format, do text-based search instead
          if (String(prov.block_id).startsWith('block_')) {
            const searchVal = String(variant[field] || variant.gene_studied || '');
            window.pdfViewer.highlightByText(searchVal, prov.page || null);
          } else {
            window.pdfViewer.highlightBlock(prov.block_id, prov.page || null);
          }
          switchRightTab('pdfPane');
        }
      });
    });
  }

  // === MOLECULAR BIOMARKERS — Card layout ===
  let activeBiomarkerIdx = -1;

  function renderBiomarkerView(container) {
    const section = extraction.other_molecular_biomarker_umbrella;
    if (!section || !section.other_molecular_biomarkers) { container.innerHTML = '<div style="padding:40px;text-align:center;color:var(--g400)">No molecular biomarkers extracted</div>'; return; }
    const conf = section.llm_confidence_score || 0;
    const confPct = Math.round(conf * 100);
    const confClass = confPct >= 90 ? 'high' : confPct >= 70 ? 'medium' : 'low';
    const confColor = confPct >= 90 ? 'var(--green)' : confPct >= 70 ? 'var(--amber)' : 'var(--cardinal-red)';

    let html = `
      <div class="section-header">
        <div><h2>🔬 MOLECULAR BIOMARKERS</h2><div class="section-subtitle">${section.other_molecular_biomarkers.length} biomarker(s)</div></div>
        <div class="confidence-meter"><div class="conf-label">LLM Confidence</div><div class="conf-value ${confClass}">${confPct}%</div><div class="confidence-bar"><div class="confidence-bar-fill" style="width:${confPct}%;background:${confColor}"></div></div></div>
      </div>
      <div style="padding:8px 12px 0"><button class="btn-report-missing" onclick="window.app.openMissingRecordForm('other_molecular_biomarker_umbrella')" title="Report a biomarker the extractor missed">+ Report missing biomarker</button></div>
      <div class="biomarker-grid">`;

    section.other_molecular_biomarkers.forEach((bm, bi) => {
      const isActive = bi === activeBiomarkerIdx;
      const resultLower = (bm.result || '').toLowerCase();
      const isPositive = resultLower.includes('positive') || resultLower.includes('high') || resultLower.includes('detected');

      const fields = Object.keys(bm).filter(k => k !== 'provenance' && k !== 'needs_review' && k !== 'review_reason');
      const unreviewedFields = fields.filter(field => {
        const fk = fieldKey('other_molecular_biomarker_umbrella', field + '[' + bi + ']');
        const dec = fieldDecisions[fk];
        return !(dec && (dec.status === 'accepted' || dec.status === 'corrected'));
      });
      const allChecked = unreviewedFields.length > 0 && unreviewedFields.every(field => {
        const fk = fieldKey('other_molecular_biomarker_umbrella', field + '[' + bi + ']');
        return checkedFields.has(fk);
      });
      const isReviewed = unreviewedFields.length === 0;

      const bmRecFieldId = `other_molecular_biomarkers[${bi}]`;
      const bmRecDec = recordDecisions[bmRecFieldId];
      const bmIsRejected = bmRecDec && bmRecDec.status === 'rejected';
      const bmRejectBtn = bmIsRejected
        ? `<button class="btn-record-undo" onclick="event.stopPropagation();window.app.undoRejectRecord('other_molecular_biomarker_umbrella',${bi})" title="Undo record rejection">↺</button>`
        : `<button class="btn-record-reject" onclick="event.stopPropagation();window.app.rejectRecord('other_molecular_biomarker_umbrella',${bi},event)" title="Reject this whole biomarker record">✕</button>`;

      html += `
        <div class="biomarker-card ${isActive ? 'active-card' : ''} ${bmIsRejected ? 'record-rejected' : ''}" data-bi="${bi}">
          <div class="biomarker-card-header" style="gap: 10px;">
            <div style="display:flex;align-items:center;gap:8px">
              <input type="checkbox" class="field-checkbox" ${isReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.handleBiomarkerCheck(${bi}, this, event)">
              <span class="bm-name">${escHtml(bm.biomarker_name || 'Unknown')}${bm.needs_review ? ' <span class="review-flag">⚠ Review</span>' : ''}${bmIsRejected ? ' <span class="record-rejected-badge">REJECTED</span>' : ''}</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <span class="bm-method">${escHtml(bm.method || '—')}</span>
              ${bmRejectBtn}
            </div>
          </div>
          <div class="biomarker-card-body">
            <div class="bm-stat"><div class="bm-stat-label">Result</div><div class="bm-stat-value ${isPositive ? 'positive' : 'negative'}">${escHtml(bm.result || '—')}</div></div>
            <div class="bm-stat"><div class="bm-stat-label">Analyte</div><div class="bm-stat-value">${escHtml(bm.analyte || '—')}</div></div>
            <div class="bm-stat"><div class="bm-stat-label">Interpretation</div><div class="bm-stat-value">${escHtml(bm.interpretation || '—')}</div></div>
            <div class="bm-stat"><div class="bm-stat-label">Genotype</div><div class="bm-stat-value">${escHtml(bm.genotype || '—')}</div></div>
            <div class="bm-stat"><div class="bm-stat-label">Ref Range</div><div class="bm-stat-value">${escHtml(bm.reference_range || '—')}</div></div>
          </div>
        </div>`;
    });
    html += '</div><div id="biomarkerDetailArea"></div>';
    container.innerHTML = html;

    // Wire card clicks
    container.querySelectorAll('.biomarker-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.target.type === 'checkbox') return;
        const bi = parseInt(card.dataset.bi);
        activeBiomarkerIdx = bi === activeBiomarkerIdx ? -1 : bi;
        renderBiomarkerView(container);
      });
    });

    if (activeBiomarkerIdx >= 0 && activeBiomarkerIdx < section.other_molecular_biomarkers.length) {
      renderBiomarkerDetail(section.other_molecular_biomarkers[activeBiomarkerIdx], activeBiomarkerIdx);
    }
  }

  function renderBiomarkerDetail(bm, bi) {
    const area = document.getElementById('biomarkerDetailArea');
    const provMap = {};
    (bm.provenance || []).forEach(p => { provMap[p.field_name] = p; });
    const fields = Object.keys(bm).filter(k => k !== 'provenance' && k !== 'needs_review' && k !== 'review_reason');

    const unreviewedFields = fields.filter(field => {
      const fk = fieldKey('other_molecular_biomarker_umbrella', field + '[' + bi + ']');
      const dec = fieldDecisions[fk];
      return !(dec && (dec.status === 'accepted' || dec.status === 'corrected'));
    });
    const allChecked = unreviewedFields.length > 0 && unreviewedFields.every(field => {
      const fk = fieldKey('other_molecular_biomarker_umbrella', field + '[' + bi + ']');
      return checkedFields.has(fk);
    });
    const isAllReviewed = unreviewedFields.length === 0;

    let html = `<div class="variant-detail-panel">
      <h3><span class="detail-icon">✎</span> Field-level Review: ${escHtml(bm.biomarker_name || '')}</h3>
      ${bm.needs_review ? `<div style="padding:8px 12px;background:var(--amber-bg);border-radius:var(--r-sm);margin-bottom:12px;font-size:.78rem;color:#92400E;font-weight:600">⚠ ${escHtml(bm.review_reason || 'Needs SME review')}</div>` : ''}
      <table class="variant-fields-table">
        <thead>
          <tr>
            <th style="width:30px;padding:6px 12px;text-align:left"><input type="checkbox" class="field-checkbox" ${isAllReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.toggleSelectAll(this)"></th>
            <th style="padding:6px 12px;text-align:left;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Field</th>
            <th style="padding:6px 12px;text-align:left;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Value</th>
            <th style="width:100px;padding:6px 12px;text-align:center;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Action</th>
          </tr>
        </thead>
        <tbody>`;
    fields.forEach(field => {
      const val = bm[field];
      const fk = fieldKey('other_molecular_biomarker_umbrella', field + '[' + bi + ']');
      const dec = fieldDecisions[fk];
      const displayVal = dec && dec.status === 'corrected' ? dec.correctedValue : (val !== null ? String(val) : null);
      const isFieldReviewed = dec && (dec.status === 'accepted' || dec.status === 'corrected');
      html += `<tr${dec ? (dec.status === 'accepted' ? ' class="row-accepted"' : ' class="row-corrected"') : ''}>
        <td><input type="checkbox" class="field-checkbox" ${isFieldReviewed ? 'disabled style="opacity:0.3"' : ''} ${checkedFields.has(fk) ? 'checked' : ''} onchange="window.app.handleFieldCheck('${escAttr(fk)}', this)"></td>
        <td class="vf-name">${escHtml(field)}</td>
        <td class="vf-value ${displayVal === null ? 'null' : ''}">${displayVal !== null ? escHtml(displayVal) : '—'}</td>
        <td class="vf-actions">${renderFieldActions(fk)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    area.innerHTML = html;

    area.querySelectorAll('.variant-fields-table tbody tr').forEach((tr, fi) => {
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', (e) => {
        if (e.target.tagName === 'BUTTON' || e.target.type === 'checkbox') return;
        const field = fields[fi];
        const prov = provMap[field] || {};
        selectedField = { field, prov, value: bm[field] };
        renderFieldDetail(selectedField);
        if (DOCS[currentDocIdx].hasPdf && prov.block_id) {
          if (String(prov.block_id).startsWith('block_')) {
            window.pdfViewer.highlightByText(String(bm[field] || bm.biomarker_name || ''), prov.page || null);
          } else {
            window.pdfViewer.highlightBlock(prov.block_id, prov.page || null);
          }
          switchRightTab('pdfPane');
        }
      });
    });
  }

  // === TESTED BIOMARKERS — Chip grid with field-level review ===
  //
  // Note (Stage 4.1b): tested_biomarkers records are scalar strings, not dicts.
  // Record-level reject / report-missing buttons are NOT added here in v1 because
  // openMissingRecordForm assumes dict-shaped records. Scalar-record support is
  // a small enhancement — see memory (parked TODO).
  function renderTestedView(container) {
    const section = extraction.tested_biomarker_umbrella;
    if (!section) { container.innerHTML = '<div style="padding:40px;text-align:center;color:var(--g400)">No tested biomarkers</div>'; return; }
    const conf = section.llm_confidence_score || 0;
    const confPct = Math.round(conf * 100);
    const confClass = confPct >= 90 ? 'high' : confPct >= 70 ? 'medium' : 'low';
    const confColor = confPct >= 90 ? 'var(--green)' : confPct >= 70 ? 'var(--amber)' : 'var(--cardinal-red)';
    const biomarkers = section.tested_biomarkers || [];
    const pages = section.page_numbers || [];

    // Fields to show in review (exclude llm_confidence_score)
    const reviewFields = Object.keys(section).filter(k => k !== 'llm_confidence_score');

    let html = `
      <div class="section-header">
        <div><h2>\ud83e\uddea TESTED BIOMARKERS</h2><div class="section-subtitle">${section.count_of_tested_biomarkers || biomarkers.length} biomarker(s) tested \u00b7 Source pages: ${pages.join(', ') || '\u2014'}</div></div>
        <div class="confidence-meter"><div class="conf-label">LLM Confidence</div><div class="conf-value ${confClass}">${confPct}%</div><div class="confidence-bar"><div class="confidence-bar-fill" style="width:${confPct}%;background:${confColor}"></div></div></div>
      </div>
      <div class="tested-grid">`;
    biomarkers.forEach(b => {
      html += `<div class="tested-chip" data-biomarker="${escHtml(b)}"><span class="chip-dot"></span>${escHtml(b)}</div>`;
    });
    html += '</div>';

    if (pages.length > 0) {
      html += '<div style="padding:12px 20px;border-top:1px solid var(--g100)"><div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--g400);font-weight:700;margin-bottom:8px">Source Pages</div><div style="display:flex;gap:6px;flex-wrap:wrap">';
      pages.forEach(p => {
        html += `<button class="tested-page-btn" data-page="${p}" style="border:1px solid var(--g200);background:var(--white);padding:6px 14px;border-radius:var(--r-sm);cursor:pointer;font-size:.8rem;font-weight:600;color:var(--g700);transition:var(--tr)">Page ${p}</button>`;
      });
      html += '</div></div>';
    }

    const unreviewedFields = reviewFields.filter(field => {
      const fk = fieldKey('tested_biomarker_umbrella', field);
      const dec = fieldDecisions[fk];
      return !(dec && (dec.status === 'accepted' || dec.status === 'corrected'));
    });
    const allChecked = unreviewedFields.length > 0 && unreviewedFields.every(field => {
      const fk = fieldKey('tested_biomarker_umbrella', field);
      return checkedFields.has(fk);
    });
    const isAllReviewed = unreviewedFields.length === 0;

    // Field-level review table
    html += `<div class="variant-detail-panel" style="margin:12px">
      <h3><span class="detail-icon">✎</span> Field-level Review</h3>
      <table class="variant-fields-table">
        <thead>
          <tr>
            <th style="width:30px;padding:6px 12px;text-align:left"><input type="checkbox" class="field-checkbox" ${isAllReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.toggleSelectAll(this)"></th>
            <th style="padding:6px 12px;text-align:left;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Field</th>
            <th style="padding:6px 12px;text-align:left;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Value</th>
            <th style="width:100px;padding:6px 12px;text-align:center;font-size:0.7rem;text-transform:uppercase;color:var(--g500)">Action</th>
          </tr>
        </thead>
        <tbody>`;
    reviewFields.forEach(field => {
      const val = section[field];
      const displayVal = Array.isArray(val) ? val.join(', ') : (val !== null ? String(val) : null);
      const fk = fieldKey('tested_biomarker_umbrella', field);
      const dec = fieldDecisions[fk];
      const shownVal = dec && dec.status === 'corrected' ? dec.correctedValue : displayVal;
      const isFieldReviewed = dec && (dec.status === 'accepted' || dec.status === 'corrected');
      html += `<tr${dec ? (dec.status === 'accepted' ? ' class="row-accepted"' : ' class="row-corrected"') : ''}>
        <td><input type="checkbox" class="field-checkbox" ${isFieldReviewed ? 'disabled style="opacity:0.3"' : ''} ${checkedFields.has(fk) ? 'checked' : ''} onchange="window.app.handleFieldCheck('${escAttr(fk)}', this)"></td>
        <td class="vf-name">${escHtml(field)}</td>
        <td class="vf-value ${shownVal === null ? 'null' : ''}">${shownVal !== null ? escHtml(shownVal) : '—'}</td>
        <td class="vf-actions">${renderFieldActions(fk)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    container.innerHTML = html;

    // Wire page buttons to navigate PDF
    container.querySelectorAll('.tested-page-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const pageNum = parseInt(btn.dataset.page);
        if (DOCS[currentDocIdx].hasPdf && window.pdfViewer) {
          window.pdfViewer.highlightBlock(null, pageNum);
          switchRightTab('pdfPane');
        }
      });
      btn.addEventListener('mouseenter', () => { btn.style.borderColor = 'var(--cardinal-red)'; btn.style.color = 'var(--cardinal-red)'; btn.style.background = 'var(--cardinal-bg)'; });
      btn.addEventListener('mouseleave', () => { btn.style.borderColor = 'var(--g200)'; btn.style.color = 'var(--g700)'; btn.style.background = 'var(--white)'; });
    });

    // Helper: search loaded blocks for text and return provenance-like object
    function findBlockProv(searchText, preferPage) {
      if (!searchText || !blocks.length) return { type: 'derived', page: preferPage || null };
      const needle = String(searchText).toLowerCase().trim();
      let best = null;
      for (const b of blocks) {
        const t = (b.text || '').toLowerCase();
        if (t.includes(needle)) {
          if (preferPage && Number(b.page_number) === Number(preferPage)) { best = b; break; }
          if (!best) best = b;
        }
      }
      if (best) {
        return { type: 'linked', block_id: best.block_id, page: Number(best.page_number), text: best.text };
      }
      return { type: 'derived', page: preferPage || null };
    }

    // Wire chip clicks to show traceability + navigate PDF
    container.querySelectorAll('.tested-chip').forEach(chip => {
      chip.style.cursor = 'pointer';
      chip.addEventListener('click', () => {
        const name = chip.dataset.biomarker;
        const prov = findBlockProv(name, pages[0] || null);
        selectedField = { field: name, prov, value: name };
        renderFieldDetail(selectedField);
        if (DOCS[currentDocIdx].hasPdf) {
          if (prov.block_id) {
            window.pdfViewer.highlightBlock(prov.block_id, prov.page);
          } else {
            window.pdfViewer.highlightByText(name, pages[0] || null);
          }
          switchRightTab('pdfPane');
        }
      });
    });

    // Wire field rows for traceability
    container.querySelectorAll('.variant-fields-table tr').forEach((tr, fi) => {
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', (e) => {
        if (e.target.tagName === 'BUTTON') return;
        const field = reviewFields[fi];
        const val = section[field];
        const displayVal = Array.isArray(val) ? val.join(', ') : val;
        // For the tested_biomarkers list field, search for the first biomarker
        const searchText = field === 'tested_biomarkers' && biomarkers.length > 0 ? biomarkers[0] : String(displayVal || '');
        const prov = findBlockProv(searchText, pages[0] || null);
        selectedField = { field, prov, value: displayVal };
        renderFieldDetail(selectedField);
        if (DOCS[currentDocIdx].hasPdf) {
          if (prov.block_id) {
            window.pdfViewer.highlightBlock(prov.block_id, prov.page);
          } else {
            window.pdfViewer.highlightByText(searchText, pages[0] || null);
          }
          switchRightTab('pdfPane');
        }
      });
    });
  }

  async function acceptField(fk, event) {
    if (event) event.stopPropagation();
    const prior = fieldDecisions[fk];
    fieldDecisions[fk] = { status: 'accepted' };   // optimistic
    renderExtractionTable();
    try {
      await saveActionToServer(fk, 'accept');
    } catch (e) {
      if (prior) fieldDecisions[fk] = prior; else delete fieldDecisions[fk];
      renderExtractionTable();
    }
  }

  function rejectField(fk, event) {
    if (event) event.stopPropagation();
    const fieldName = fk.split('::')[1];
    const panel = document.getElementById('fieldDetail');
    panel.className = 'field-detail';
    panel.innerHTML = `
      <h4>✎ Correct: ${escHtml(fieldName)}</h4>
      <div style="margin-bottom:10px;font-size:.8rem;color:var(--g500)">Enter the correct value for this field:</div>
      <input type="text" class="sme-input" id="correctionInput" placeholder="Enter corrected value…" autofocus>
      <div class="btn-group" style="margin-top:8px">
        <button class="btn btn-accept" onclick="window.app.submitCorrection('${escAttr(fk)}')">Save Correction</button>
        <button class="btn" style="background:var(--g100);color:var(--g600)" onclick="window.app.cancelCorrection()">Cancel</button>
      </div>
    `;
    setTimeout(() => { const inp = document.getElementById('correctionInput'); if (inp) inp.focus(); }, 50);
  }

  async function submitCorrection(fk) {
    const input = document.getElementById('correctionInput');
    const val = input ? input.value.trim() : '';
    if (!val) return;
    // Original value = whatever the field currently shows in the extraction
    // (best-effort — used for audit trail, not for merge logic).
    const originalValue = selectedField && selectedField.field
      ? String(selectedField.value ?? '')
      : null;
    const prior = fieldDecisions[fk];
    fieldDecisions[fk] = { status: 'corrected', correctedValue: val };   // optimistic
    renderExtractionTable();
    renderFieldDetail(null);
    try {
      await saveActionToServer(fk, 'correct', {
        originalValue: originalValue,
        correctedValue: val,
      });
    } catch (e) {
      if (prior) fieldDecisions[fk] = prior; else delete fieldDecisions[fk];
      renderExtractionTable();
    }
  }

  function cancelCorrection() { renderFieldDetail(selectedField); }

  function selectRow(tr, fieldName, prov, value) {
    document.querySelectorAll('.extraction-table tbody tr').forEach(r => r.classList.remove('selected'));
    tr.classList.add('selected');
    selectedField = { field: fieldName, prov, value };
    renderFieldDetail(selectedField);
    if (prov && prov.block_id && DOCS[currentDocIdx].hasPdf) {
      if (String(prov.block_id).startsWith('block_')) {
        window.pdfViewer.highlightByText(String(value || fieldName || ''), prov.page || null);
      } else {
        window.pdfViewer.highlightBlock(prov.block_id, prov.page || null);
      }
      switchRightTab('pdfPane');
    } else if (window.pdfViewer) {
      window.pdfViewer.clearHighlight();
    }
  }

  function renderFieldDetail(sel) {
    const panel = document.getElementById('fieldDetail');
    if (!sel) { panel.className = 'field-detail empty'; panel.innerHTML = '<span>← Click a field row to see traceability details</span>'; return; }
    panel.className = 'field-detail';
    const { field, prov, value } = sel;
    const fk = fieldKey(currentSection, field);
    const dec = fieldDecisions[fk];
    const blockInfo = prov.block_id ? blocks.find(b => String(b.block_id) === String(prov.block_id)) : null;
    const blockText = blockInfo ? blockInfo.text : '';
    const decisionHtml = dec ? `<div class="label">SME Status</div><div class="value"><span class="field-status ${dec.status}">${dec.status === 'accepted' ? '✓ Accepted' : '✎ Corrected → ' + escHtml(dec.correctedValue)}</span></div>` : '';
    panel.innerHTML = `
      <h4>${escHtml(field)} <span class="prov-badge ${prov.type || 'absent'}">${prov.type || 'absent'}</span></h4>
      <div class="detail-grid">
        <div class="label">Extracted</div><div class="value">${value !== null ? escHtml(String(value)) : '<em style="color:var(--g400)">null</em>'}</div>
        ${decisionHtml}
        ${prov.block_id ? `<div class="label">Block ID</div><div class="value">Block ${prov.block_id} · Page ${prov.page || '?'}</div>` : ''}
        ${prov.type ? `<div class="label">Source Type</div><div class="value">${prov.type}</div>` : ''}
        ${prov.rationale ? `<div class="label">Rationale</div><div class="value rationale">${escHtml(prov.rationale)}</div>` : ''}
        ${blockText ? `<div class="label">Block Text</div><div class="value rationale">${escHtml(blockText)}</div>` : ''}
      </div>
    `;
  }

  function renderVerifiers() {
    const list = document.getElementById('verifierList');
    if (!list) return;
    if (!verificationData || !verificationData.scorecards) return;
    list.innerHTML = '';
    verificationData.scorecards.forEach(sc => {
      const div = document.createElement('div');
      div.className = 'verifier-badge';
      div.innerHTML = `<span>${escHtml(sc.verifier_name)}</span><span class="v-status ${sc.passed ? 'pass' : 'fail'}">${sc.passed ? 'PASS' : 'FAIL'}</span>`;
      list.appendChild(div);
    });
  }

  function renderEscalations() {
    const panel = document.getElementById('escalationPanel');
    if (!escalationData) { panel.innerHTML = '<div class="escalation-empty"><div class="empty-icon">📭</div>No escalation data</div>'; return; }
    panel.innerHTML = '';
    const bands = escalationData.bands || {};
    const order = escalationData.band_order || Object.keys(bands);
    let totalItems = 0;
    order.forEach(band => {
      const items = (bands[band] || []).filter(it => !EXCLUDED_KINDS.has(it.kind || it.defect_type));
      totalItems += items.length;
      const section = document.createElement('div');
      section.className = 'escalation-band';
      const bandColors = { judgment: 'var(--cardinal-red)', unresolved: 'var(--amber)', review_light: 'var(--blue)', drop_audit: 'var(--g400)' };
      section.innerHTML = `<h4><span style="width:8px;height:8px;border-radius:50%;background:${bandColors[band] || 'var(--g400)'};display:inline-block"></span> ${escHtml(band.replace(/_/g, ' '))} <span class="band-count">${items.length}</span></h4>`;
      if (items.length === 0) {
        const empty = document.createElement('div');
        empty.style.cssText = 'font-size:.78rem;color:var(--g400);padding:4px 0';
        empty.textContent = 'No items in this band';
        section.appendChild(empty);
      }
      const bandSev = { judgment: 'absent', unresolved: 'inferred', review_light: 'derived', drop_audit: 'inferred' };
      items.forEach(item => {
        if (!item.triage_band) item.triage_band = band;
        const card = document.createElement('div');
        card.className = 'escalation-item';
        card.style.cursor = 'pointer';
        card.title = 'Click to locate this in the PDF and see why it was escalated';
        const kind = item.kind || item.defect_type || '';
        const kindBadge = kind ? `<span class="prov-badge ${bandSev[band] || 'derived'}" style="margin-left:8px">${escHtml(kind)}</span>` : '';
        const reason = item.reason || item.detail || item.defect_type || '';
        card.innerHTML = `<strong>${escHtml(item.ref || item.field || 'Unknown')}${kindBadge}</strong>`
          + `<div style="font-size:.75rem;color:var(--g500);margin-top:4px;line-height:1.4">${escHtml(reason)}</div>`
          + `<div style="font-size:.7rem;color:var(--blue);margin-top:5px;font-weight:600">▶ locate &amp; explain</div>`;
        card.onclick = () => focusEscalation(item);
        section.appendChild(card);
      });
      panel.appendChild(section);
    });
    if (totalItems === 0) {
      panel.innerHTML += '<div class="escalation-empty"><div class="empty-icon">✅</div>No escalations — all fields passed quality checks</div>';
    }
  }

  // ── Escalation focus: click an escalation → locate in PDF + explain ──────────
  // Maps a banded escalation item (from pipeline/vmaw.py: ref/section/kind/detail/
  // reason + triage_band/vmaw_note/vmaw_proposal) to: PDF highlight + page jump,
  // the reason, what the SME must check, and the agent's reasoning.

  const RECORD_ARRAYS = {
    Genomic_Variant_umbrella: 'Genomic_Variants',
    other_molecular_biomarker_umbrella: 'other_molecular_biomarkers',
    significant_findings: 'specimen_findings',
  };

  // What the SME should actually do, keyed by VMAW defect kind.
  const KIND_GUIDANCE = {
    binding_refuted: 'The cited evidence may not actually contain this value (possible hallucination or synonym/canonicalization gap). Confirm the highlighted block really states it — if not, correct the value or reject.',
    dropped_ungroundable: 'This record was DROPPED because no source could be grounded. Confirm it is genuinely absent from the document, or restore it if it really appears.',
    invalid_hgvs: 'The variant nomenclature failed HGVS validation. Verify the c./p./g. notation against the report and correct it.',
    block_misroute: 'This value may belong to a different section. Check whether the highlighted block was assigned to the right section.',
    link_cannot_form: 'An expected relationship between two records could not be formed. Check whether the two records actually relate.',
    binding_unconfirmed: 'The value-to-source binding could not be confirmed. Verify the highlighted evidence supports the value.',
  };
  // Defect kinds that must NOT surface as SME escalations in the UI.
  const EXCLUDED_KINDS = new Set(['schema_error', 'normalization_invalid']);
  // Extra guidance from the triage band (priority/disposition).
  const BAND_GUIDANCE = {
    judgment: 'Decision required — pick the correct value, ground it yourself, or accept/reject the proposed value.',
    unresolved: 'Investigate from scratch — the auto-resolver found no answer for this field.',
    review_light: 'Low-risk — a grounded, uncontested proposal held back from auto-apply. Quick confirm.',
    drop_audit: 'Audit a deletion — confirm the record is truly absent, or restore it.',
  };

  function parseRef(item) {
    let ref = String(item.ref || item.field || '');
    // Real data sometimes stores a stringified Python list, e.g. "['Genomic_Variant_umbrella']".
    const listish = ref.match(/^\[\s*'?([^'\]]+)'?\s*\]$/);
    if (listish) ref = listish[1];
    const section = item.section || (ref.includes('.') ? ref.split('.')[0] : ref) || currentSection;
    let recordIndex = 0;
    const m = ref.match(/\[(\d+)\]/);
    if (m) recordIndex = parseInt(m[1], 10);
    const last = ref.includes('.') ? ref.split('.').pop() : ref;
    let fieldName = (last || '').replace(/\[\d+\]/g, '');
    // Section-level or empty ref → fall back to the section name as the label.
    if (!fieldName || fieldName === section) fieldName = section || ref || 'item';
    return { ref, section, fieldName, recordIndex };
  }

  // Best-effort provenance lookup: returns {block_id, page, type, rationale, value}.
  function provFor(section, fieldName, recordIndex) {
    if (!extraction) return null;
    let provArr = null, value = null;
    if (section === 'report_metadata') {
      const meta = extraction.report_metadata || {};
      provArr = meta.provenance || [];
      value = meta[fieldName];
    } else {
      const arrName = RECORD_ARRAYS[section];
      const recs = arrName && extraction[section] ? extraction[section][arrName] : null;
      const rec = Array.isArray(recs) ? recs[recordIndex || 0] : null;
      if (rec) { provArr = rec.provenance || []; value = rec[fieldName]; }
    }
    if (!Array.isArray(provArr)) provArr = [];
    const entry = provArr.find(p => p && String(p.field_name) === String(fieldName));
    if (entry) return Object.assign({}, entry, { value });
    return value != null ? { value } : null;
  }

  function pageOfBlock(blockId) {
    if (blockId == null || !Array.isArray(blocks)) return null;
    const norm = String(blockId).replace(/^block_/i, '');
    const b = blocks.find(x => String(x.block_id).replace(/^block_/i, '') === norm);
    return b ? b.page_number : null;
  }

  function focusEscalation(item) {
    const { ref, section, fieldName, recordIndex } = parseRef(item);

    // 1) Center panel → switch to the owning section so the field row is visible.
    const KNOWN = ['report_metadata', 'Genomic_Variant_umbrella', 'other_molecular_biomarker_umbrella', 'tested_biomarker_umbrella'];
    if (KNOWN.includes(section)) {
      document.querySelectorAll('.section-tab').forEach(t => t.classList.toggle('active', t.dataset.section === section));
      currentSection = section;
      selectedField = null;
      checkedFields.clear();
      renderExtractionTable();
      updateBulkActionBar();
    }

    // 2) Resolve where it lives in the PDF (vmaw citation → provenance → text).
    const prov = provFor(section, fieldName, recordIndex) || {};
    const vmawCites = (item.vmaw_proposal && item.vmaw_proposal.citation_block_ids) || item.citation_block_ids || [];
    const blockId = (vmawCites && vmawCites[0]) || prov.block_id || null;
    const page = prov.page || pageOfBlock(blockId) || item.page || null;
    const value = (item.vmaw_proposal && item.vmaw_proposal.value) || prov.value || null;

    // 3) PDF → highlight the entity and auto-jump to its page.
    const hasPdf = DOCS[currentDocIdx] && DOCS[currentDocIdx].hasPdf && window.pdfViewer;
    if (hasPdf) {
      if (blockId && !String(blockId).startsWith('block_')) window.pdfViewer.highlightBlock(blockId, page);
      else if (value) window.pdfViewer.highlightByText(String(value), page);
      else if (blockId) window.pdfViewer.highlightBlock(blockId, page);
      else if (page) window.pdfViewer.highlightBlock(null, page);
      switchRightTab('pdfPane');
    }

    // 4) Agent reasoning: per-field rationale + the most relevant trace step.
    const fieldReasoning = prov.rationale || '';
    const traceHit = (agentTraceData || []).filter(s =>
      s && (s.team === section || s.section === section || (s.plain || '').includes(fieldName) || (s.reasoning || '').includes(fieldName))
    ).pop();
    const traceReasoning = traceHit && traceHit.reasoning ? traceHit.reasoning : '';

    // 5) Render the explanation (reason + what-to-check + reasoning) in the
    //    center detail panel, so it stays visible alongside the PDF.
    showEscalationDetail(item, { ref, section, fieldName, value, blockId, page, fieldReasoning, traceReasoning, traceAgent: traceHit ? traceHit.agent : '' });
  }

  function showEscalationDetail(item, ctx) {
    const panel = document.getElementById('fieldDetail');
    if (!panel) return;
    panel.className = 'field-detail';
    const band = item.triage_band || '';
    const kind = item.kind || item.defect_type || '';
    const reason = item.reason || item.detail || '';
    const whatToCheck = [KIND_GUIDANCE[kind], BAND_GUIDANCE[band]].filter(Boolean).join(' ');
    const rows = [];
    rows.push(`<div class="label">Field</div><div class="value">${escHtml(ctx.ref || ctx.fieldName)}</div>`);
    if (kind) rows.push(`<div class="label">Defect kind</div><div class="value">${escHtml(kind)}</div>`);
    if (band) rows.push(`<div class="label">Triage band</div><div class="value">${escHtml(band.replace(/_/g, ' '))}</div>`);
    if (ctx.value != null) rows.push(`<div class="label">Extracted</div><div class="value">${escHtml(String(ctx.value))}</div>`);
    if (reason) rows.push(`<div class="label">Reason for escalation</div><div class="value rationale">${escHtml(reason)}</div>`);
    if (whatToCheck) rows.push(`<div class="label">What to check</div><div class="value rationale">${escHtml(whatToCheck)}</div>`);
    if (ctx.blockId || ctx.page) rows.push(`<div class="label">Source</div><div class="value">Block ${escHtml(String(ctx.blockId || '?'))} · Page ${escHtml(String(ctx.page || '?'))}</div>`);
    if (ctx.fieldReasoning) rows.push(`<div class="label">Agent reasoning (field)</div><div class="value rationale">${escHtml(ctx.fieldReasoning)}</div>`);
    if (ctx.traceReasoning) rows.push(`<div class="label">Agent reasoning${ctx.traceAgent ? ' (' + escHtml(ctx.traceAgent) + ')' : ''}</div><div class="value rationale">${escHtml(String(ctx.traceReasoning).substring(0, 600))}</div>`);
    const fk = fieldKey(ctx.section, ctx.fieldName);
    panel.innerHTML = `<h4>⚠ Escalation — ${escHtml(ctx.fieldName || 'item')}</h4>`
      + `<div class="detail-grid">${rows.join('')}</div>`
      + `<div class="btn-group" style="margin-top:10px">`
      + `<button class="btn btn-accept" onclick="window.app.acceptField('${escAttr(fk)}', event)">✓ Accept</button>`
      + `<button class="btn btn-reject" onclick="window.app.rejectField('${escAttr(fk)}', event)">✎ Correct</button>`
      + `<button class="btn btn-null" onclick="window.app.rejectFieldValue('${escAttr(fk)}', event)" title="Reject: mark this field as wrongly extracted (value becomes null in ground truth)">✕ Reject</button>`
      + `<button class="btn" style="background:var(--g100);color:var(--g600)" onclick="window.app.switchRightTab('tracePane')">🔍 Full agent trace</button>`
      + `</div>`;
  }

  function switchRightTab(paneId) {
    document.querySelectorAll('.right-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === paneId));
    document.querySelectorAll('.right-pane').forEach(p => p.classList.toggle('active', p.id === paneId));
  }

  // Kept as a no-op stub for backwards compat (nothing in the HTML references it now).
  function smeDecision(decision) { console.warn('smeDecision is deprecated — use submitDocument'); }

  // ── Doc-level Submit workflow (Q12 A, FR-4, FR-8) ─────────────────────
  //
  // Two-step flow:
  //   1. POST /submit with force=false.
  //        - status="submitted"        → write banner, refresh state
  //        - status="confirm_required" → show modal listing untouched fields;
  //                                      Cancel or Submit Anyway (force=true).
  //   2. If Submit Anyway: POST /submit with force=true → banner + refresh.
  //
  // Uses the existing #smeReviewer input as the reviewer identity (T2 placeholder
  // for real auth). Requires reviewer name before submitting.

  async function submitDocument() {
    const docId = currentDocId();
    if (!docId) return;
    const reviewerId = document.getElementById('smeReviewer').value.trim();
    const msgEl = document.getElementById('smeMsg');
    if (!reviewerId) {
      if (msgEl) msgEl.textContent = 'Reviewer name is required before submitting.';
      document.getElementById('smeReviewer').focus();
      return;
    }
    if (msgEl) msgEl.textContent = 'Submitting…';

    try {
      const r = await fetch(`/api/review/${encodeURIComponent(docId)}/submit`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ force: false, reviewer_id: reviewerId }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const result = await r.json();

      if (result.status === 'confirm_required') {
        openSubmitConfirmModal(docId, reviewerId, result);
        if (msgEl) msgEl.textContent = '';
      } else if (result.status === 'submitted') {
        // Fix (user feedback): after submit, return to the queue automatically.
        await refreshDashboardData();
        showDashboard();
      }
    } catch (e) {
      showSaveError(`Submit failed (${e.message || e})`);
      if (msgEl) msgEl.textContent = '';
    }
  }

  function openSubmitConfirmModal(docId, reviewerId, result) {
    const modal = document.getElementById('submitConfirmModal');
    if (!modal) return;
    // User feedback: show ALL unreviewed fields in a scrollable list, each
    // clickable to jump to the specific field in the doc-review view.
    const list = result.unreviewed_fields || [];
    const listItems = list.map(fid => `
      <li>
        <button class="unreviewed-item" onclick="window.app.jumpToUnreviewedField('${escAttr(fid)}')" title="Jump to this field">
          <span class="ui-icon">→</span>
          <span class="ui-fid">${escHtml(fid)}</span>
        </button>
      </li>
    `).join('');
    modal.innerHTML = `
      <div class="mr-scrim" onclick="window.app.cancelSubmitConfirm()"></div>
      <div class="mr-dialog sc-dialog" role="dialog" aria-labelledby="scTitle">
        <h3 id="scTitle">Submit ${escHtml(docId)}?</h3>
        <p style="font-size:.86rem;color:var(--g700);margin:0 0 12px">
          You haven't reviewed <b>${result.unreviewed_count}</b> field(s).
          Click any item below to jump to that field, or <b>Submit anyway</b> to
          implicitly accept them all.
        </p>
        <div class="sc-list-header">
          <span>Unreviewed fields (${list.length})</span>
          <input type="text" id="scFilter" placeholder="Filter…" oninput="window.app.filterUnreviewedList(this.value)">
        </div>
        <ul class="unreviewed-list" id="unreviewedList">
          ${listItems}
        </ul>
        <div class="mr-actions">
          <button type="button" class="btn" onclick="window.app.cancelSubmitConfirm()">Cancel</button>
          <button type="button" class="btn btn-accept" onclick="window.app.confirmSubmit('${escAttr(docId)}','${escAttr(reviewerId)}')">Submit anyway (${result.unreviewed_count} implicit accepts)</button>
        </div>
      </div>
    `;
    modal.className = 'missing-record-modal visible';
  }

  function filterUnreviewedList(q) {
    const needle = (q || '').toLowerCase().trim();
    document.querySelectorAll('#unreviewedList li').forEach(li => {
      const txt = (li.textContent || '').toLowerCase();
      li.style.display = (!needle || txt.includes(needle)) ? '' : 'none';
    });
  }

  // ── jumpToUnreviewedField(serverFieldId) ──────────────────────────────
  //
  // Called from the scrollable unreviewed-list. Given a server field_id like
  // "Genomic_Variants[3].amino_acid_change" or "report_metadata.Patient_MRN":
  //   1. Close the submit-confirm modal
  //   2. Switch to the section tab containing the field
  //   3. For record-scoped fields, expand the specific record (activeVariantIdx / activeBiomarkerIdx)
  //   4. Scroll the field row into view + pulse-highlight for ~2s

  function jumpToUnreviewedField(serverFieldId) {
    cancelSubmitConfirm();
    if (!serverFieldId) return;

    // Determine target section from the field_id shape
    let targetSection = null;
    let recordIndex = -1;
    let fieldName = null;

    if (serverFieldId.startsWith('report_metadata.')) {
      targetSection = 'report_metadata';
      fieldName = serverFieldId.substring('report_metadata.'.length);
    } else {
      const m = serverFieldId.match(/^(\w+)\[(\d+)\](?:\.(.+))?$/);
      if (m) {
        const [, listKey, idxStr, fname] = m;
        targetSection = LIST_KEY_TO_SECTION[listKey] || null;
        recordIndex = parseInt(idxStr, 10);
        fieldName = fname || null;
      }
    }

    if (!targetSection) {
      showSaveError(`Could not parse field id: ${serverFieldId}`);
      return;
    }

    // Set the active record BEFORE switching section — the section renderer
    // consults activeVariantIdx / activeBiomarkerIdx on render.
    if (targetSection === 'Genomic_Variant_umbrella' && recordIndex >= 0) {
      activeVariantIdx = recordIndex;
    } else if (targetSection === 'other_molecular_biomarker_umbrella' && recordIndex >= 0) {
      activeBiomarkerIdx = recordIndex;
    }

    // Switch section tab
    document.querySelectorAll('.section-tab').forEach(t =>
      t.classList.toggle('active', t.dataset.section === targetSection)
    );
    currentSection = targetSection;
    selectedField = null;
    checkedFields.clear();
    renderExtractionTable();
    renderFieldDetail(null);
    updateBulkActionBar();

    // After render completes, find the target field row and scroll to it.
    // Use requestAnimationFrame so the DOM is up-to-date before we query it.
    requestAnimationFrame(() => {
      const row = _findFieldRow(targetSection, recordIndex, fieldName);
      if (row) {
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.classList.add('field-row-flash');
        setTimeout(() => row.classList.remove('field-row-flash'), 2200);
      }
    });
  }

  function _findFieldRow(section, recordIndex, fieldName) {
    // report_metadata: the extraction table has field-name cells; find one that matches
    if (section === 'report_metadata' && fieldName) {
      const rows = document.querySelectorAll('#extractionBody tr');
      for (const tr of rows) {
        const nameCell = tr.querySelector('.field-name');
        if (nameCell && nameCell.textContent.trim() === fieldName) return tr;
      }
      return null;
    }
    // Record-scoped sections: field rows live in the variant-fields-table
    // (rendered by renderVariantDetail / renderBiomarker equivalents).
    if (fieldName) {
      const rows = document.querySelectorAll('.variant-fields-table tbody tr');
      for (const tr of rows) {
        const nameCell = tr.querySelector('.vf-name');
        if (nameCell && nameCell.textContent.trim() === fieldName) return tr;
      }
    }
    return null;
  }

  function cancelSubmitConfirm() {
    const modal = document.getElementById('submitConfirmModal');
    if (modal) { modal.className = 'missing-record-modal'; modal.innerHTML = ''; }
  }

  async function confirmSubmit(docId, reviewerId) {
    cancelSubmitConfirm();
    const msgEl = document.getElementById('smeMsg');
    if (msgEl) msgEl.textContent = 'Submitting…';
    try {
      const r = await fetch(`/api/review/${encodeURIComponent(docId)}/submit`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ force: true, reviewer_id: reviewerId }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const result = await r.json();
      if (result.status === 'submitted') {
        // Fix (user feedback): after submit, return to the queue automatically
        // so the SME immediately sees the updated aggregate metrics.
        await refreshDashboardData();
        showDashboard();
      }
    } catch (e) {
      showSaveError(`Submit failed (${e.message || e})`);
      if (msgEl) msgEl.textContent = '';
    }
  }

  async function reopenDocument() {
    const docId = currentDocId();
    if (!docId) return;
    const msgEl = document.getElementById('smeMsg');
    if (msgEl) msgEl.textContent = 'Reopening…';
    try {
      const r = await fetch(`/api/review/${encodeURIComponent(docId)}/reopen`, { method: 'POST' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await refreshAfterSubmit(docId);
      if (msgEl) msgEl.textContent = 'Reopened for editing.';
    } catch (e) {
      showSaveError(`Reopen failed (${e.message || e})`);
      if (msgEl) msgEl.textContent = '';
    }
  }

  async function refreshAfterSubmit(docId) {
    // Reload state + re-render whatever section is currently visible.
    await hydrateReviewStateFromServer(docId);
    renderSubmittedBanner();
    updateSubmitButtonState();
    renderExtractionTable();
    rerenderCurrentSection();
  }

  function renderSubmittedBanner() {
    const banner = document.getElementById('submittedBanner');
    if (!banner) return;
    const dot = document.getElementById('smeStatusDot');
    const text = document.getElementById('smeStatusText');
    // Pull the latest submission info from a state fetch
    const docId = currentDocId();
    if (!docId) return;
    fetch(`/api/review/${encodeURIComponent(docId)}/state`).then(r => r.json()).then(bundle => {
      const state = bundle.state;
      const metrics = bundle.metrics || {};
      if (state.submission_status === 'submitted') {
        const acc = Math.round((metrics.accuracy || 0) * 100);
        const prec = Math.round((metrics.precision || 0) * 100);
        const rec = Math.round((metrics.recall || 0) * 100);
        const submittedAt = state.submitted_at ? state.submitted_at.replace('T', ' ').replace('Z', ' UTC') : '';
        banner.innerHTML = `
          <div class="banner-line">✓ Submitted on ${escHtml(submittedAt)} by <b>${escHtml(state.submitted_by || '?')}</b></div>
          <div class="banner-metrics">Accuracy ${acc}% · Precision ${prec}% · Recall ${rec}%</div>
          <button class="btn btn-reopen" onclick="window.app.reopenDocument()">↺ Re-open for editing</button>
        `;
        banner.className = 'submitted-banner visible';
        if (dot) dot.className = 'status-dot accepted';
        if (text) text.textContent = 'Submitted';
      } else if (state.actions && state.actions.length > 0) {
        banner.className = 'submitted-banner';
        banner.innerHTML = '';
        if (dot) dot.className = 'status-dot';
        if (text) text.textContent = 'Draft';
      } else {
        banner.className = 'submitted-banner';
        banner.innerHTML = '';
        if (dot) dot.className = 'status-dot';
        if (text) text.textContent = 'Not started';
      }
    }).catch(() => {});
  }

  function updateSubmitButtonState() {
    // Called after refreshAfterSubmit. Nothing needed for v1 — button is always visible.
    // Reserved for future: disable-during-submit spinner, etc.
  }

  // Refresh the cached allDocsData so the dashboard reflects the latest state
  // (metrics, submission status, escalation counts). Called after any submit
  // so the queue view is up-to-date when we navigate back.
  async function refreshDashboardData() {
    if (!DOCS || !DOCS.length) return;
    allDocsData = [];
    for (const doc of DOCS) {
      try {
        const [esc, trace, ver] = await Promise.all([
          loadJSON(doc.dir, 'escalation_queue_banded.json'),
          loadJSON(doc.dir, 'agent_trace.json'),
          loadJSON(doc.dir, 'verification_v2.json').catch(() => null),
        ]);
        const lastStep = trace[trace.length - 1];
        allDocsData.push({
          id: doc.id, label: doc.label, dir: doc.dir,
          escalation: esc, verdict: lastStep ? lastStep.verdict : 'unknown',
          verification: ver,
        });
      } catch (e) { console.warn('refreshDashboardData: failed for', doc.id, e); }
    }
  }

  function onBlockClick(blockId) {
    if (!extraction || !extraction.report_metadata) return;
    const prov = (extraction.report_metadata.provenance || []).find(p => String(p.block_id) === String(blockId));
    if (prov) {
      document.querySelectorAll('.section-tab').forEach(t => t.classList.remove('active'));
      document.querySelector('[data-section="report_metadata"]').classList.add('active');
      currentSection = 'report_metadata';
      renderExtractionTable();
      document.querySelectorAll('.extraction-table tbody tr').forEach(r => {
        if (r.querySelector('.field-name')?.textContent === prov.field_name) r.click();
      });
    }
  }

  function escHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
  function escAttr(s) { return s.replace(/'/g, "\\'").replace(/"/g, '&quot;'); }

  // ── Bulk Review & Checkbox Management ────────────────────────
  function isReviewed(fk) {
    const dec = fieldDecisions[fk];
    // Include 'rejected' — a rejected field IS reviewed; SME made an explicit call.
    return dec && (dec.status === 'accepted' || dec.status === 'corrected' || dec.status === 'rejected');
  }

  function getVisibleUnreviewedFieldKeys() {
    if (!extraction) return [];
    if (currentSection === 'report_metadata') {
      const meta = extraction.report_metadata;
      if (!meta) return [];
      return Object.keys(meta)
        .filter(k => !HIDDEN_META_FIELDS.includes(k))
        .map(field => fieldKey('report_metadata', field))
        .filter(fk => !isReviewed(fk));
    }
    if (currentSection === 'Genomic_Variant_umbrella') {
      if (activeVariantIdx < 0) return [];
      const section = extraction.Genomic_Variant_umbrella;
      if (!section || !section.Genomic_Variants) return [];
      const variant = section.Genomic_Variants[activeVariantIdx];
      if (!variant) return [];
      const fields = Object.keys(variant).filter(k => k !== 'provenance' && k !== 'hgvs_normalized' && k !== 'needs_review' && k !== 'review_reason');
      return fields.map(field => fieldKey('Genomic_Variant_umbrella', field + '[' + activeVariantIdx + ']')).filter(fk => !isReviewed(fk));
    }
    if (currentSection === 'other_molecular_biomarker_umbrella') {
      if (activeBiomarkerIdx < 0) return [];
      const section = extraction.other_molecular_biomarker_umbrella;
      if (!section || !section.other_molecular_biomarkers) return [];
      const bm = section.other_molecular_biomarkers[activeBiomarkerIdx];
      if (!bm) return [];
      const fields = Object.keys(bm).filter(k => k !== 'provenance' && k !== 'needs_review' && k !== 'review_reason');
      return fields.map(field => fieldKey('other_molecular_biomarker_umbrella', field + '[' + activeBiomarkerIdx + ']')).filter(fk => !isReviewed(fk));
    }
    if (currentSection === 'tested_biomarker_umbrella') {
      const section = extraction.tested_biomarker_umbrella;
      if (!section) return [];
      const reviewFields = Object.keys(section).filter(k => k !== 'llm_confidence_score');
      return reviewFields.map(field => fieldKey('tested_biomarker_umbrella', field)).filter(fk => !isReviewed(fk));
    }
    return [];
  }

  function updateSelectAllCheckboxState() {
    const chk = document.getElementById('selectAllFields');
    if (!chk) return;
    const keys = getVisibleUnreviewedFieldKeys();
    const allChecked = keys.length > 0 && keys.every(fk => checkedFields.has(fk));
    chk.checked = allChecked;
    updateBulkActionBar();
  }

  function getCheckedFieldsForCurrentSection() {
    const prefix = currentSection + '::';
    return Array.from(checkedFields).filter(fk => fk.startsWith(prefix));
  }

  function updateBulkActionBar() {
    const bar = document.getElementById('bulkActionBar');
    if (!bar) return;
    const currentChecked = getCheckedFieldsForCurrentSection();
    if (currentChecked.length > 0) {
      bar.style.display = 'flex';
      bar.innerHTML = `
        <div><span id="bulkCheckedCount">${currentChecked.length}</span> field(s) selected</div>
        <div class="bar-right">
          <button class="btn-bulk-accept" onclick="window.app.bulkAccept()">✓ Bulk Accept</button>
          <button class="btn-bulk-cancel" onclick="window.app.bulkCancel()">Clear</button>
        </div>
      `;
    } else {
      bar.style.display = 'none';
      bar.innerHTML = '';
    }
  }

  function rerenderCurrentSection() {
    renderExtractionTable();
  }

  function toggleSelectAll(chk) {
    const keys = getVisibleUnreviewedFieldKeys();
    if (chk.checked) {
      keys.forEach(fk => checkedFields.add(fk));
    } else {
      keys.forEach(fk => checkedFields.delete(fk));
    }
    rerenderCurrentSection();
    updateSelectAllCheckboxState();
  }

  function handleFieldCheck(fk, chk) {
    if (chk.checked) {
      checkedFields.add(fk);
    } else {
      checkedFields.delete(fk);
    }
    updateSelectAllCheckboxState();
    rerenderCurrentSection();
  }

  function handleVariantCheck(vi, chk) {
    const section = extraction.Genomic_Variant_umbrella;
    if (!section || !section.Genomic_Variants) return;
    const variant = section.Genomic_Variants[vi];
    if (!variant) return;
    const fields = Object.keys(variant).filter(k => k !== 'provenance' && k !== 'hgvs_normalized' && k !== 'needs_review' && k !== 'review_reason');
    fields.forEach(field => {
      const fk = fieldKey('Genomic_Variant_umbrella', field + '[' + vi + ']');
      if (!isReviewed(fk)) {
        if (chk.checked) checkedFields.add(fk);
        else checkedFields.delete(fk);
      }
    });
    rerenderCurrentSection();
    updateBulkActionBar();
  }

  function handleBiomarkerCheck(bi, chk, event) {
    if (event) event.stopPropagation();
    const section = extraction.other_molecular_biomarker_umbrella;
    if (!section || !section.other_molecular_biomarkers) return;
    const bm = section.other_molecular_biomarkers[bi];
    if (!bm) return;
    const fields = Object.keys(bm).filter(k => k !== 'provenance' && k !== 'needs_review' && k !== 'review_reason');
    fields.forEach(field => {
      const fk = fieldKey('other_molecular_biomarker_umbrella', field + '[' + bi + ']');
      if (!isReviewed(fk)) {
        if (chk.checked) checkedFields.add(fk);
        else checkedFields.delete(fk);
      }
    });
    rerenderCurrentSection();
    updateBulkActionBar();
  }

  async function bulkAccept() {
    const currentChecked = getCheckedFieldsForCurrentSection();
    // Optimistic local update first — no partial-state flicker
    currentChecked.forEach(fk => {
      fieldDecisions[fk] = { status: 'accepted' };
      checkedFields.delete(fk);
    });
    rerenderCurrentSection();
    updateSelectAllCheckboxState();
    // Fire the POSTs in parallel — order doesn't matter, server dedupes by field_id
    try {
      await Promise.all(currentChecked.map(fk => saveActionToServer(fk, 'accept')));
    } catch (e) {
      // One or more failed; user already saw the error banner via saveActionToServer.
      // Leave the optimistic state — user can re-Accept individual failed ones.
      console.warn('bulkAccept: some saves failed', e);
    }
  }

  function bulkCancel() {
    const currentChecked = getCheckedFieldsForCurrentSection();
    currentChecked.forEach(fk => {
      checkedFields.delete(fk);
    });
    rerenderCurrentSection();
    updateSelectAllCheckboxState();
  }

  window.app = {
    init,
    switchRightTab,
    smeDecision,
    onBlockClick,
    acceptField,
    rejectField,
    rejectFieldValue,             // Stage 4.1 — real reject (field → null)
    undoFieldDecision,            // Fix: re-open editing — clear a prior decision
    rejectRecord,                 // Stage 4.1b — reject whole record
    undoRejectRecord,             // Stage 4.1b — undo record rejection
    openMissingRecordForm,        // Stage 4.1b — open add-missing form
    cancelMissingRecordForm,      // Stage 4.1b — close add-missing form
    submitMissingRecordForm,      // Stage 4.1b — form submit handler
    submitDocument,               // Stage 4.2 — Q12 A: repurposed Submit button
    cancelSubmitConfirm,          // Stage 4.2 — Cancel in the confirm dialog
    confirmSubmit,                // Stage 4.2 — Submit Anyway in the confirm dialog
    reopenDocument,               // Stage 4.2 — Re-open a submitted doc
    jumpToUnreviewedField,        // Fix: scrollable list — click item to jump
    filterUnreviewedList,         // Fix: scrollable list — text-filter input
    loadDocumentById,             // Stage 4.3 — for dashboard quick-open by ID
    quickSubmitDoc,               // Stage 4.3 — per-row Submit button on dashboard
    submitCorrection,
    cancelCorrection,
    focusEscalation,
    toggleSelectAll,
    handleFieldCheck,
    handleVariantCheck,
    handleBiomarkerCheck,
    bulkAccept,
    bulkCancel,
    showDashboard,
    loadDocument,
    filterQueue
  };
  document.addEventListener('DOMContentLoaded', init);
})();

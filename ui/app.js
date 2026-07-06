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
  let checkedFields = new Set();
  let allDocsData = [];

  async function loadJSON(dir, file) {
    const r = await fetch(dir + file);
    return r.json();
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

    // Load all documents for aggregate stats
    for (const doc of DOCS) {
      try {
        const [esc, trace] = await Promise.all([
          loadJSON(doc.dir, 'escalation_queue_banded.json'),
          loadJSON(doc.dir, 'agent_trace.json')
        ]);
        const lastStep = trace[trace.length - 1];
        allDocsData.push({ id: doc.id, escalation: esc, verdict: lastStep ? lastStep.verdict : 'unknown' });
      } catch (e) { console.warn('Could not load stats for', doc.id, e); }
    }

    renderDocList();
    renderAggregateStats();
    await loadDocument(0);
  }

  async function loadDocument(idx) {
    currentDocIdx = idx;
    const doc = DOCS[idx];
    fieldDecisions = {};
    checkedFields.clear();
    selectedField = null;
    currentSection = 'report_metadata';

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

  function renderDocList() {
    const list = document.getElementById('docList');
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
    document.getElementById('statTotal').textContent = DOCS.length;
    document.getElementById('statAccepted').textContent = autoAccepted;
    document.getElementById('bandJudgment').textContent = aggBands.judgment;
    document.getElementById('bandUnresolved').textContent = aggBands.unresolved;
    document.getElementById('bandReviewLight').textContent = aggBands.review_light;
    document.getElementById('bandDropAudit').textContent = aggBands.drop_audit;
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
    if (dec && dec.status === 'accepted') return `<span class="field-status accepted">✓ Accepted</span>`;
    if (dec && dec.status === 'corrected') return `<span class="field-status corrected" title="Corrected to: ${escHtml(dec.correctedValue)}">✎ Corrected</span>`;
    return `<button class="btn-field-accept" onclick="window.app.acceptField('${escAttr(fk)}', event)">✓</button><button class="btn-field-reject" onclick="window.app.rejectField('${escAttr(fk)}', event)">✎</button>`;
  }

  function buildRow(section, field, val, prov, tbody) {
    const fk = fieldKey(section, field);
    const dec = fieldDecisions[fk];
    const tr = document.createElement('tr');
    if (dec && dec.status === 'accepted') tr.classList.add('row-accepted');
    if (dec && dec.status === 'corrected') tr.classList.add('row-corrected');
    const isReviewed = dec && (dec.status === 'accepted' || dec.status === 'corrected');
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
      <div style="padding:0 12px"><table class="variant-summary" id="variantSummaryTable">
        <thead><tr><th style="width:30px"></th><th>Gene</th><th>Result</th><th>Coding Change</th><th>AA Change</th><th>Significance</th><th style="width:60px"></th></tr></thead>
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

      tr.innerHTML = `
        <td><input type="checkbox" class="field-checkbox" ${isReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.handleVariantCheck(${vi}, this)"></td>
        <td class="gene-cell">${escHtml(v.gene_studied || '—')}</td>
        <td class="result-cell ${isDetected ? 'result-detected' : 'result-not-detected'}">${escHtml(v.result || '—')}</td>
        <td class="change-cell">${escHtml(v.coding_dna_change || '—')}</td>
        <td class="change-cell">${escHtml(v.amino_acid_change || '—')}</td>
        <td class="sig-cell" title="${escHtml(v.clinical_significance || '')}">${escHtml((v.clinical_significance || '—').substring(0, 50))}${(v.clinical_significance||'').length > 50 ? '…' : ''}</td>
        <td>${v.needs_review ? '<span class="review-flag">⚠ Review</span>' : ''}</td>
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

      html += `
        <div class="biomarker-card ${isActive ? 'active-card' : ''}" data-bi="${bi}">
          <div class="biomarker-card-header" style="gap: 10px;">
            <div style="display:flex;align-items:center;gap:8px">
              <input type="checkbox" class="field-checkbox" ${isReviewed ? 'disabled style="opacity:0.3"' : ''} ${allChecked ? 'checked' : ''} onchange="window.app.handleBiomarkerCheck(${bi}, this, event)">
              <span class="bm-name">${escHtml(bm.biomarker_name || 'Unknown')}${bm.needs_review ? ' <span class="review-flag">⚠ Review</span>' : ''}</span>
            </div>
            <span class="bm-method">${escHtml(bm.method || '—')}</span>
          </div>
          <div class="biomarker-card-body">
            <div class="bm-stat"><div class="bm-stat-label">Result</div><div class="bm-stat-value ${isPositive ? 'positive' : 'negative'}">${escHtml(bm.result || '—')}</div></div>
            <div class="bm-stat"><div class="bm-stat-label">Interpretation</div><div class="bm-stat-value">${escHtml(bm.interpretation || '—')}</div></div>
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

  function acceptField(fk, event) {
    if (event) event.stopPropagation();
    fieldDecisions[fk] = { status: 'accepted' };
    renderExtractionTable();
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

  function submitCorrection(fk) {
    const input = document.getElementById('correctionInput');
    const val = input ? input.value.trim() : '';
    if (!val) return;
    fieldDecisions[fk] = { status: 'corrected', correctedValue: val };
    renderExtractionTable();
    renderFieldDetail(null);
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
      + `<button class="btn" style="background:var(--g100);color:var(--g600)" onclick="window.app.switchRightTab('tracePane')">🔍 Full agent trace</button>`
      + `</div>`;
  }

  function switchRightTab(paneId) {
    document.querySelectorAll('.right-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === paneId));
    document.querySelectorAll('.right-pane').forEach(p => p.classList.toggle('active', p.id === paneId));
  }

  function smeDecision(decision) {
    const name = document.getElementById('smeReviewer').value.trim();
    if (!name) { document.getElementById('smeMsg').textContent = 'Reviewer name is required'; return; }
    const dot = document.getElementById('smeStatusDot');
    const text = document.getElementById('smeStatusText');
    if (decision === 'accept') { dot.className = 'status-dot accepted'; text.textContent = 'Accepted'; }
    else { dot.className = 'status-dot rejected'; text.textContent = 'Rejected'; }
    document.getElementById('smeMsg').textContent = `Decision by ${name} at ${new Date().toLocaleTimeString()}`;
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

  window.app = { init, switchRightTab, smeDecision, onBlockClick, acceptField, rejectField, submitCorrection, cancelCorrection, focusEscalation };
  document.addEventListener('DOMContentLoaded', init);
})();

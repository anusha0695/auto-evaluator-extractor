/**
 * Agent Trace Timeline — Visualizes extraction pipeline steps
 */
(function () {
  const PHASE_LABELS = {
    preprocess: '🔧 Preprocess', planning: '📋 Planning', extraction: '⚙ Extraction',
    linking: '🔗 Linking', verification: '✅ Verification', triage: '🔀 Triage',
    repair: '🔨 Repair', decision: '🏁 Decision'
  };

  function render(traceData, containerEl) {
    if (!traceData || !traceData.length) {
      containerEl.innerHTML = '<div class="escalation-empty"><div class="empty-icon">📭</div>No agent trace data</div>';
      return;
    }
    // Group by phase
    const phases = {};
    traceData.forEach(step => {
      const p = step.phase || 'unknown';
      if (!phases[p]) phases[p] = [];
      phases[p].push(step);
    });

    containerEl.innerHTML = '';
    // Summary bar
    const summary = document.createElement('div');
    summary.style.cssText = 'padding:8px 12px;background:var(--white);border-radius:var(--r-sm);margin-bottom:12px;font-size:.78rem;border:1px solid var(--g200);display:flex;gap:16px;flex-wrap:wrap';
    summary.innerHTML = `<span><strong>${traceData.length}</strong> steps</span>` +
      Object.entries(phases).map(([p, s]) => `<span style="opacity:.7">${PHASE_LABELS[p] || p}: ${s.length}</span>`).join('');
    containerEl.appendChild(summary);

    Object.entries(phases).forEach(([phase, steps]) => {
      const group = document.createElement('div');
      group.className = `phase-group phase-${phase}`;
      const isExtraction = phase === 'extraction';
      group.innerHTML = `
        <div class="phase-header" onclick="this.querySelector('.chevron').classList.toggle('open');this.nextElementSibling.classList.toggle('collapsed')">
          <span class="chevron ${isExtraction ? '' : 'open'}">▶</span>
          <span>${PHASE_LABELS[phase] || phase}</span>
          <span class="phase-count">${steps.length} step${steps.length > 1 ? 's' : ''}</span>
        </div>
        <div class="phase-steps ${isExtraction ? 'collapsed' : ''}"></div>
      `;
      const stepsContainer = group.querySelector('.phase-steps');
      steps.forEach(step => {
        const el = document.createElement('div');
        el.className = 'trace-step';
        el.onclick = () => el.classList.toggle('expanded');
        const meta = [];
        if (step.team) meta.push(`team: ${step.team}`);
        if (step.latency_ms > 0) meta.push(`${(step.latency_ms / 1000).toFixed(1)}s`);
        if (step.tool_calls > 0) meta.push(`${step.tool_calls} tool calls`);
        if (step.confidence !== null && step.confidence !== undefined) meta.push(`conf: ${step.confidence}`);
        const reasoning = step.reasoning ? step.reasoning.substring(0, 500) : '';
        el.innerHTML = `
          <div class="step-header">
            <span class="step-num">#${step.step}</span>
            <span class="step-agent">${escHtml(step.agent)}</span>
            <span class="step-verdict verdict-${step.verdict}">${step.verdict}</span>
          </div>
          <div class="step-desc">${escHtml(step.plain || '')}</div>
          ${meta.length ? `<div class="step-meta">${meta.map(m => `<span>${escHtml(m)}</span>`).join('')}</div>` : ''}
          ${reasoning ? `<div class="step-reasoning">${escHtml(reasoning)}</div>` : ''}
        `;
        stepsContainer.appendChild(el);
      });
      containerEl.appendChild(group);
    });
  }

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  window.agentTrace = { render };
})();

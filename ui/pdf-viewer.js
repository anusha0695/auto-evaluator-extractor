/**
 * PDF Viewer Module — Renders PDF with block highlight overlays
 */
(function () {
  let dataDir = '/artifacts/';
  let pdfDoc = null, currentPage = 1, scale = 1.8, blocksData = [], showBlocks = true;
  let highlightedBlockId = null;

  const canvas = document.getElementById('pdfCanvas');
  const ctx = canvas.getContext('2d');
  const container = document.getElementById('pdfPageContainer');
  const overlaysDiv = document.getElementById('blockOverlays');

  async function init(dir) {
    if (dir) dataDir = dir;
    try {
      const res = await fetch(dataDir + 'blocks.json');
      blocksData = await res.json();
    } catch (e) { console.warn('Could not load blocks.json', e); blocksData = []; }

    pdfjsLib.GlobalWorkerOptions.workerSrc =
      'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

    // Restore the canvas container
    const wrap = document.getElementById('pdfCanvasWrap');
    if (!wrap.querySelector('#pdfPageContainer')) {
      wrap.innerHTML = '<div class="pdf-page-container" id="pdfPageContainer"><canvas id="pdfCanvas"></canvas><div id="blockOverlays"></div></div>';
    }

    try {
      pdfDoc = await pdfjsLib.getDocument(dataDir + 'source.pdf').promise;
      renderPage(1);
    } catch (e) { console.error('PDF load failed', e); }
  }

  async function renderPage(num) {
    if (!pdfDoc) return;
    currentPage = num;
    document.getElementById('pageNum').textContent = num;
    document.getElementById('prevPage').disabled = num <= 1;
    document.getElementById('nextPage').disabled = num >= pdfDoc.numPages;

    const c = document.getElementById('pdfCanvas');
    const cx = c.getContext('2d');
    const cont = document.getElementById('pdfPageContainer');
    const ovl = document.getElementById('blockOverlays');

    const page = await pdfDoc.getPage(num);
    const viewport = page.getViewport({ scale });
    c.width = viewport.width;
    c.height = viewport.height;
    cont.style.width = viewport.width + 'px';
    cont.style.height = viewport.height + 'px';

    await page.render({ canvasContext: cx, viewport }).promise;
    renderBlockOverlays(num, viewport.width, viewport.height, ovl);
  }

  function renderBlockOverlays(pageNum, w, h, ovl) {
    if (!ovl) ovl = document.getElementById('blockOverlays');
    ovl.innerHTML = '';
    if (!showBlocks) return;
    // Use Number() to handle string/int page_number mismatch
    const pageBlocks = blocksData.filter(b => Number(b.page_number) === Number(pageNum) && b.bbox);
    pageBlocks.forEach(block => {
      const [x1, y1, x2, y2] = block.bbox;
      const div = document.createElement('div');
      div.className = 'block-overlay';
      if (highlightedBlockId && normalizeBlockId(block.block_id) === highlightedBlockId) div.classList.add('highlighted');
      div.style.left = (x1 * w) + 'px';
      div.style.top = (y1 * h) + 'px';
      div.style.width = ((x2 - x1) * w) + 'px';
      div.style.height = ((y2 - y1) * h) + 'px';
      div.title = `Block ${block.block_id}: ${(block.text || '').substring(0, 80)}`;
      div.dataset.blockId = block.block_id;
      div.addEventListener('click', () => {
        if (window.app && window.app.onBlockClick) window.app.onBlockClick(block.block_id);
      });
      ovl.appendChild(div);
    });
  }

  // Normalize block IDs: "block_11" → "11", "11" → "11", 11 → "11"
  function normalizeBlockId(id) {
    if (id == null) return null;
    return String(id).replace(/^block_/i, '');
  }

  function highlightBlock(blockId, pageNum) {
    highlightedBlockId = blockId != null ? normalizeBlockId(blockId) : null;
    if (pageNum && Number(pageNum) !== currentPage) {
      renderPage(Number(pageNum));
    } else {
      const c = document.getElementById('pdfCanvas');
      if (c) renderBlockOverlays(currentPage, c.width, c.height);
    }
    setTimeout(() => {
      const ovl = document.getElementById('blockOverlays');
      if (ovl) { const el = ovl.querySelector('.highlighted'); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    }, 100);
  }

  /**
   * Text-based block search fallback.
   * When provenance block_id doesn't map correctly (e.g. "block_11" naming),
   * search block text for the field value and highlight the matching block.
   */
  function highlightByText(searchText, pageNum) {
    if (!searchText || !blocksData.length) return;
    const pg = pageNum ? Number(pageNum) : null;
    const needle = String(searchText).toLowerCase().trim();
    // Search for best match: prefer exact page, then any page
    let best = null;
    for (const b of blocksData) {
      const t = (b.text || '').toLowerCase();
      if (t.includes(needle)) {
        if (pg && Number(b.page_number) === pg) { best = b; break; }
        if (!best) best = b;
      }
    }
    if (best) {
      highlightBlock(best.block_id, best.page_number);
    } else if (pg) {
      // No text match — at least navigate to the page
      highlightBlock(null, pg);
    }
  }

  function clearHighlight() {
    highlightedBlockId = null;
    const c = document.getElementById('pdfCanvas');
    if (c) renderBlockOverlays(currentPage, c.width, c.height);
  }

  function nextPage() { if (pdfDoc && currentPage < pdfDoc.numPages) renderPage(currentPage + 1); }
  function prevPage() { if (pdfDoc && currentPage > 1) renderPage(currentPage - 1); }
  function zoomBy(delta) { scale = Math.max(0.5, Math.min(3, scale + delta)); renderPage(currentPage); }
  function toggleBlocks() {
    showBlocks = !showBlocks;
    const btn = document.getElementById('blockToggle');
    btn.classList.toggle('on', showBlocks);
    const c = document.getElementById('pdfCanvas');
    if (c) renderBlockOverlays(currentPage, c.width, c.height);
  }

  window.pdfViewer = { init, highlightBlock, highlightByText, clearHighlight, nextPage, prevPage, zoom: zoomBy, toggleBlocks, getBlocksData: () => blocksData };
})();

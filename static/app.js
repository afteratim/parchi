const form = document.getElementById('uploadForm');
const imagesInput = document.getElementById('images');
const fileList = document.getElementById('fileList');
const statusBox = document.getElementById('status');
const extractBtn = document.getElementById('extractBtn');
const resultsSection = document.getElementById('resultsSection');
const pagesContainer = document.getElementById('pagesContainer');
const recordCount = document.getElementById('recordCount');
const uncertainOnly = document.getElementById('uncertainOnly');
let pages = [];

imagesInput.addEventListener('change', () => {
  const files = [...imagesInput.files];
  fileList.textContent = files.length ? `${files.length} image${files.length === 1 ? '' : 's'} selected: ${files.map(f => f.name).join(', ')}` : 'No images selected yet.';
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!imagesInput.files.length) return;
  const body = new FormData();
  [...imagesInput.files].forEach(file => body.append('images', file));
  extractBtn.disabled = true;
  statusBox.className = 'status';
  statusBox.textContent = 'Processing images. Large photos may take a little while…';
  try {
    const response = await fetch('/api/extract', {method: 'POST', body});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Extraction failed.');
    pages = data.pages || [];
    renderPages();
    resultsSection.classList.remove('hidden');
    const count = pages.reduce((sum, p) => sum + (p.records || []).length, 0);
    statusBox.textContent = `Finished. ${pages.length} image(s), ${count} record(s) detected. Please review the results.`;
    if (!count) statusBox.textContent += ' OCR did not confidently identify any GR rows; check the raw text below and try a clearer photo.';
    resultsSection.scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    statusBox.className = 'status error';
    statusBox.textContent = error.message || 'Something went wrong.';
  } finally {
    extractBtn.disabled = false;
  }
});

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function flattenRecords() {
  const all = [];
  pages.forEach((page, pi) => (page.records || []).forEach((record, ri) => all.push({...record, _pi: pi, _ri: ri, date: record.date || page.date || ''})));
  return all;
}
function outputLine(record) {
  const date = String(record.date || '').trim();
  const ctns = String(record.ctns || '').trim();
  return `${String(record.gr || '').trim()},"${date} - ${ctns} CRTNS"`;
}
function renderPages() {
  const all = flattenRecords();
  recordCount.textContent = `${all.length} record${all.length === 1 ? '' : 's'}`;
  pagesContainer.innerHTML = pages.map((page, pi) => {
    const records = (page.records || []).map((r, ri) => ({...r, _pi:pi, _ri:ri, date:r.date || page.date || ''}));
    const visible = uncertainOnly.checked ? records.filter(r => r.uncertain || !r.gr || !r.ctns) : records;
    const tableRows = visible.map(r => {
      const rowClass = `${r.uncertain || !r.ctns || !r.gr ? 'uncertain' : ''} ${r.duplicate ? 'duplicate' : ''}`;
      const flag = r.duplicate ? '<span class="flag dup">DUPLICATE</span>' : (r.uncertain || !r.ctns || !r.gr ? '<span class="flag">CHECK</span>' : '<span class="flag ok">OK</span>');
      return `<tr class="${rowClass}" data-pi="${r._pi}" data-ri="${r._ri}">
        <td><input aria-label="GR number" data-field="gr" value="${esc(r.gr)}" /></td>
        <td><input aria-label="CTNS" data-field="ctns" inputmode="numeric" value="${esc(r.ctns)}" /></td>
        <td><input aria-label="Date" data-field="date" placeholder="DD-MM-YYYY" value="${esc(r.date)}" /></td>
        <td><input aria-label="Heading" data-field="heading" value="${esc(r.heading)}" /></td>
        <td>${flag}<div class="output-preview">${esc(outputLine(r))}</div></td>
        <td><button class="secondary small remove-row" type="button" title="Remove this row from export only">Exclude</button></td>
      </tr>`;
    }).join('');
    return `<article class="page-card"><div class="page-title"><div><strong>${esc(page.filename)}</strong><small>${records.length} detected row(s)${page.error ? ' · OCR error: '+esc(page.error) : ''}</small></div><label class="date-edit">Page date <input data-page-date="${pi}" placeholder="DD-MM-YYYY" value="${esc(page.date || '')}" /></label></div>
      ${records.length ? `<div class="table-wrap"><table><thead><tr><th>GR number</th><th>CTNS</th><th>Date</th><th>Heading / section</th><th>Review & output</th><th>Action</th></tr></thead><tbody>${tableRows || '<tr><td colspan="6">No rows match this filter.</td></tr>'}</tbody></table></div>` : '<div style="padding:16px;color:#667085;font-size:12px">No GR rows detected on this page.</div>'}
      <details style="padding:14px 18px"><summary style="cursor:pointer;font-size:12px;color:#667085">Show OCR text for troubleshooting</summary><pre style="white-space:pre-wrap;font-size:11px;color:#475467">${esc(page.raw_text || 'No text detected.')}</pre></details></article>`;
  }).join('');
  // A page with no visible rows still retains all records in memory for filtering toggles.
  pagesContainer.querySelectorAll('input[data-field]').forEach(input => input.addEventListener('change', () => {
    const row = input.closest('tr');
    const pi = Number(row.dataset.pi), ri = Number(row.dataset.ri), field = input.dataset.field;
    pages[pi].records[ri][field] = input.value.trim();
    if (field === 'gr' || field === 'ctns') pages[pi].records[ri].uncertain = !pages[pi].records[ri].gr || !pages[pi].records[ri].ctns;
    updateDuplicates();
    renderPages();
  }));
  pagesContainer.querySelectorAll('input[data-page-date]').forEach(input => input.addEventListener('change', () => {
    const pi = Number(input.dataset.pageDate); pages[pi].date = input.value.trim();
    (pages[pi].records || []).forEach(r => { if (!r.date) r.date = input.value.trim(); });
    renderPages();
  }));
  pagesContainer.querySelectorAll('.remove-row').forEach(button => button.addEventListener('click', () => {
    const row = button.closest('tr');
    const pi = Number(row.dataset.pi), ri = Number(row.dataset.ri);
    pages[pi].records[ri].excluded = !pages[pi].records[ri].excluded;
    button.textContent = pages[pi].records[ri].excluded ? 'Include' : 'Exclude';
    row.style.opacity = pages[pi].records[ri].excluded ? '.45' : '1';
    row.querySelectorAll('input').forEach(i => i.disabled = pages[pi].records[ri].excluded);
  }));
}
function updateDuplicates() {
  const seen = new Set();
  pages.forEach(p => (p.records || []).forEach(r => {
    const key = `${r.date || p.date || ''}|${String(r.gr || '').trim()}`;
    r.duplicate = Boolean(r.gr) && seen.has(key);
    if (r.gr) seen.add(key);
  }));
}
uncertainOnly.addEventListener('change', renderPages);
function activeRecords() { return flattenRecords().filter(r => !pages[r._pi].records[r._ri].excluded); }
function copyOutput() {
  const lines = activeRecords().map(outputLine).filter(line => !line.startsWith(',"'));
  if (!lines.length) { statusBox.textContent = 'No complete GR rows to copy.'; return; }
  navigator.clipboard.writeText(lines.join('\n')).then(() => { statusBox.textContent = `Copied ${lines.length} output line(s).`; }).catch(() => { statusBox.textContent = 'Clipboard access was blocked by the browser. Use Download CSV instead.'; });
}
async function downloadCsv() {
  const records = activeRecords();
  const response = await fetch('/api/export', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({records})});
  if (!response.ok) { statusBox.textContent = 'CSV export failed.'; return; }
  const blob = await response.blob(); const url = URL.createObjectURL(blob); const a = document.createElement('a');
  a.href = url; a.download = 'parchi_extraction.csv'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  statusBox.textContent = `CSV downloaded with ${records.length} record(s).`;
}
document.getElementById('copyBtn').addEventListener('click', copyOutput);
document.getElementById('copyBtnBottom').addEventListener('click', copyOutput);
document.getElementById('csvBtn').addEventListener('click', downloadCsv);
document.getElementById('csvBtnBottom').addEventListener('click', downloadCsv);

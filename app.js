'use strict';

const $ = (id) => document.getElementById(id);
let accessToken = '';
let viewer = null;
let currentImages = [];
let currentIndex = -1;
let currentRunMeta = null;
let shortlistIds = new Set();
let metadataCache = new Map();
let aiJobId = null;
let aiPollTimer = null;
let aiJobRunId = null;
let originalDistanceOrder = [];
let targetConfigured = false;
let currentOrderMode = 'distance';
let largeAreaJobId = null;
let largeAreaPollTimer = null;
let largeAreaJobsCache = new Map();
let lastLargeAreaActiveCellId = null;
let lastAiScoredIds = new Set();
let lastAiImageType = 'all';
let aiImageTypeCounts = {all:0,pano:0,flat:0,unknown:0};
let lastAiJobStats = null;

const els = {
  tokenBadge: $('tokenBadge'), searchMode: $('searchMode'), centerLat: $('centerLat'), centerLng: $('centerLng'), radiusM: $('radiusM'), largeRadiusM: $('largeRadiusM'),
  standardRadiusLabel: $('standardRadiusLabel'), largeRadiusLabel: $('largeRadiusLabel'), standardSearchActions: $('standardSearchActions'), largeAreaPanel: $('largeAreaPanel'),
  imageType: $('imageType'), startCapturedAt: $('startCapturedAt'), endCapturedAt: $('endCapturedAt'), maxApiImages: $('maxApiImages'), maxApiLabel: $('maxApiLabel'),
  discoverBtn: $('discoverBtn'), discoverStatus: $('discoverStatus'), runName: $('runName'), savedRuns: $('savedRuns'),
  largeJobName: $('largeJobName'), largeStartBtn: $('largeStartBtn'), largeStatus: $('largeStatus'), largeJobs: $('largeJobs'),
  largePauseBtn: $('largePauseBtn'), largeCancelBtn: $('largeCancelBtn'), largeResumeBtn: $('largeResumeBtn'), largeRetryBtn: $('largeRetryBtn'), largeLoadBtn: $('largeLoadBtn'), largeDeleteBtn: $('largeDeleteBtn'),
  largeProgress: $('largeProgress'), largeProgressText: $('largeProgressText'), largeStats: $('largeStats'), largeCellTable: $('largeCellTable'),
  saveRunBtn: $('saveRunBtn'), loadRunBtn: $('loadRunBtn'), deleteRunBtn: $('deleteRunBtn'), exportRunBtn: $('exportRunBtn'),
  shortlistCount: $('shortlistCount'), loadShortlistBtn: $('loadShortlistBtn'), exportShortlistBtn: $('exportShortlistBtn'), clearShortlistBtn: $('clearShortlistBtn'),
  candidateCounter: $('candidateCounter'), viewerMode: $('viewerMode'), mlyViewer: $('mlyViewer'), thumbViewer: $('thumbViewer'), thumbImage: $('thumbImage'), thumbStatus: $('thumbStatus'),
  prevBtn: $('prevBtn'), nextBtn: $('nextBtn'), jumpInput: $('jumpInput'), jumpBtn: $('jumpBtn'), shortlistBtn: $('shortlistBtn'), openMapillaryBtn: $('openMapillaryBtn'), copyIdBtn: $('copyIdBtn'), metadata: $('metadata'),
  aiReadyBadge: $('aiReadyBadge'), aiMode: $('aiMode'), aiImageType: $('aiImageType'), aiImageTypeSummary: $('aiImageTypeSummary'), aiThumbSize: $('aiThumbSize'), aiDevice: $('aiDevice'), aiMaxImages: $('aiMaxImages'), aiKeepCache: $('aiKeepCache'),
  aiCheckBtn: $('aiCheckBtn'), aiRankBtn: $('aiRankBtn'), aiCancelBtn: $('aiCancelBtn'), aiSortBtn: $('aiSortBtn'), distanceSortBtn: $('distanceSortBtn'), aiClearCacheBtn: $('aiClearCacheBtn'),
  aiProgress: $('aiProgress'), aiProgressText: $('aiProgressText'), aiStatus: $('aiStatus'), aiTop: $('aiTop'), orderStatus: $('orderStatus'),
  targetFile: $('targetFile'), uploadTargetBtn: $('uploadTargetBtn'), targetImage: $('targetImage'), targetLink: $('targetLink'),
  targetPlaceholder: $('targetPlaceholder'), targetStatus: $('targetStatus'), targetName: $('targetName')
};

async function api(url, options={}) {
  const res = await fetch(url, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...(options.headers||{})}});
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) throw new Error(body?.error || `${res.status} ${res.statusText}`);
  return body;
}

function paramsFromForm() {
  const latText = els.centerLat.value.trim();
  const lngText = els.centerLng.value.trim();
  return {
    centerLat: latText === '' ? null : Number(latText),
    centerLng: lngText === '' ? null : Number(lngText),
    radiusM: Number(els.radiusM.value),
    imageType: els.imageType.value,
    startCapturedAt: els.startCapturedAt.value || '',
    endCapturedAt: els.endCapturedAt.value || '',
    maxApiImages: Number(els.maxApiImages.value || 20000),
  };
}

function applyParams(p) {
  if (!p) return;
  els.centerLat.value = p.centerLat ?? '';
  els.centerLng.value = p.centerLng ?? '';
  if ((p.searchMode === 'large' || Number(p.radiusM) > 1500) && els.largeRadiusM) {
    els.searchMode.value = 'large';
    els.largeRadiusM.value = p.radiusM ?? p.overallRadiusM ?? '';
    updateSearchMode();
  } else {
    els.radiusM.value = p.radiusM;
  }
  els.imageType.value = p.imageType || 'all';
  els.startCapturedAt.value = p.startCapturedAt || '';
  els.endCapturedAt.value = p.endCapturedAt || '';
  els.maxApiImages.value = p.maxApiImages || 20000;
}

function setStatus(text, kind='') {
  els.discoverStatus.textContent = text;
  els.discoverStatus.className = `status ${kind}`.trim();
}

function formatDate(ms) {
  if (ms == null) return 'n/a';
  const d = new Date(Number(ms));
  return Number.isNaN(d.getTime()) ? String(ms) : d.toISOString().replace('T',' ').replace('.000Z',' UTC');
}

function num(v, digits=1) { return v == null ? 'n/a' : Number(v).toFixed(digits); }
function yesno(v) { return v == null ? 'n/a' : (v ? 'yes' : 'no'); }

function currentImage() { return currentIndex >= 0 && currentIndex < currentImages.length ? currentImages[currentIndex] : null; }

function mapillaryUrl(id) {
  return `https://www.mapillary.com/app/?pKey=${encodeURIComponent(id)}&focus=photo`;
}

async function init() {
  try {
    const settings = await api('/api/settings');
    accessToken = settings.accessToken || '';
    if (settings.tokenConfigured) {
      els.tokenBadge.textContent = 'Mapillary token configured';
      els.tokenBadge.className = 'badge';
    } else {
      els.tokenBadge.textContent = 'Mapillary token NOT configured';
      els.tokenBadge.className = 'badge bad';
    }
  } catch (err) {
    els.tokenBadge.textContent = 'Settings error';
    els.tokenBadge.className = 'badge bad';
  }
  await Promise.all([refreshRuns(), refreshShortlist(), refreshAiEnvironment(false), refreshTargetStatus(), refreshLargeAreaJobs()]);
  bindEvents();
  updateSearchMode();
  updateOrderStatus();
  updateAiImageTypeControls();
  updateButtons();
}

function bindEvents() {
  els.discoverBtn.addEventListener('click', discover);
  els.searchMode.addEventListener('change', updateSearchMode);
  els.largeStartBtn.addEventListener('click', startLargeAreaSearch);
  els.largeJobs.addEventListener('change', selectLargeAreaJob);
  els.largePauseBtn.addEventListener('click', pauseLargeAreaSearch);
  els.largeCancelBtn.addEventListener('click', cancelLargeAreaSearch);
  els.largeResumeBtn.addEventListener('click', resumeLargeAreaSearch);
  els.largeRetryBtn.addEventListener('click', retryLargeAreaFailed);
  els.largeLoadBtn.addEventListener('click', loadLargeAreaResults);
  els.largeDeleteBtn.addEventListener('click', deleteLargeAreaJob);
  els.prevBtn.addEventListener('click', () => showIndex(currentIndex - 1));
  els.nextBtn.addEventListener('click', () => showIndex(currentIndex + 1));
  els.jumpBtn.addEventListener('click', () => showIndex(Number(els.jumpInput.value) - 1));
  els.jumpInput.addEventListener('keydown', e => { if (e.key === 'Enter') showIndex(Number(els.jumpInput.value) - 1); });
  els.viewerMode.addEventListener('change', renderCurrent);
  els.shortlistBtn.addEventListener('click', toggleShortlist);
  els.openMapillaryBtn.addEventListener('click', () => { const im=currentImage(); if(im) window.open(mapillaryUrl(im.id),'_blank','noopener'); });
  els.copyIdBtn.addEventListener('click', async () => { const im=currentImage(); if(im) { await navigator.clipboard.writeText(im.id); setStatus(`Copied image ID ${im.id}.`,'ok'); } });
  els.saveRunBtn.addEventListener('click', saveRun);
  els.loadRunBtn.addEventListener('click', loadSelectedRun);
  els.deleteRunBtn.addEventListener('click', deleteSelectedRun);
  els.exportRunBtn.addEventListener('click', exportCurrentRun);
  els.loadShortlistBtn.addEventListener('click', loadShortlist);
  els.exportShortlistBtn.addEventListener('click', exportShortlist);
  els.clearShortlistBtn.addEventListener('click', clearShortlist);
  els.aiCheckBtn.addEventListener('click', () => refreshAiEnvironment(true));
  els.aiImageType.addEventListener('change', () => { updateAiImageTypeControls(false); updateButtons(); });
  els.aiRankBtn.addEventListener('click', startAiRanking);
  els.aiCancelBtn.addEventListener('click', cancelAiRanking);
  els.aiSortBtn.addEventListener('click', sortByAiSimilarity);
  els.distanceSortBtn.addEventListener('click', restoreDistanceOrder);
  els.aiClearCacheBtn.addEventListener('click', clearAiCache);
  els.uploadTargetBtn.addEventListener('click', uploadTargetImage);
  els.targetFile.addEventListener('change', () => {
    const f = els.targetFile.files?.[0];
    if (f) { els.targetStatus.textContent = `Selected ${f.name}. Click Use selected target.`; els.targetStatus.className = 'target-status'; }
  });
}

async function refreshTargetStatus() {
  try {
    const data = await api('/api/target');
    targetConfigured = !!data.configured;
    if (targetConfigured) {
      const v = encodeURIComponent(data.updatedAt || Date.now());
      const url = `/api/target/image?v=${v}`;
      els.targetImage.src = url;
      els.targetLink.href = url;
      els.targetLink.classList.remove('hidden');
      els.targetPlaceholder.classList.add('hidden');
      els.targetName.textContent = data.filename || 'Uploaded target image';
      els.targetStatus.textContent = `Target ready${data.sizeBytes ? ` · ${(data.sizeBytes/1024/1024).toFixed(2)} MB` : ''}.`;
      els.targetStatus.className = 'target-status ok';
    } else {
      els.targetImage.removeAttribute('src');
      els.targetLink.classList.add('hidden');
      els.targetPlaceholder.classList.remove('hidden');
      els.targetName.textContent = 'No target image selected';
      els.targetStatus.textContent = 'No target image uploaded.';
      els.targetStatus.className = 'target-status';
    }
  } catch (err) {
    targetConfigured = false;
    els.targetStatus.textContent = `Target status error: ${err.message}`;
    els.targetStatus.className = 'target-status error';
  }
  updateButtons();
  return targetConfigured;
}

async function verifyTargetImageDecodes(file) {
  if (file.size > 30 * 1024 * 1024) throw new Error('Target image is too large. Maximum size is 30 MB.');
  try {
    if (typeof createImageBitmap === 'function') {
      const bitmap = await createImageBitmap(file);
      const ok = bitmap.width > 0 && bitmap.height > 0;
      if (typeof bitmap.close === 'function') bitmap.close();
      if (!ok) throw new Error('Image dimensions are invalid.');
      return;
    }
  } catch (err) {
    throw new Error('The selected image is corrupt or cannot be decoded. Please choose another JPEG, PNG, or WebP image.');
  }
  await new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); (img.naturalWidth > 0 && img.naturalHeight > 0) ? resolve() : reject(new Error('invalid dimensions')); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode failed')); };
    img.src = url;
  }).catch(() => { throw new Error('The selected image is corrupt or cannot be decoded. Please choose another JPEG, PNG, or WebP image.'); });
}

async function uploadTargetImage() {
  const file = els.targetFile.files?.[0];
  if (!file) {
    els.targetStatus.textContent = 'Choose a target image first.';
    els.targetStatus.className = 'target-status error';
    return;
  }
  const allowed = new Set(['image/jpeg','image/png','image/webp']);
  if (!allowed.has(file.type)) {
    els.targetStatus.textContent = 'Target must be a JPEG, PNG, or WebP image.';
    els.targetStatus.className = 'target-status error';
    return;
  }
  els.uploadTargetBtn.disabled = true;
  els.targetStatus.textContent = `Validating ${file.name}…`;
  els.targetStatus.className = 'target-status';
  try {
    await verifyTargetImageDecodes(file);
    els.targetStatus.textContent = `Uploading ${file.name}…`;
    const res = await fetch('/api/target', {
      method: 'POST',
      cache: 'no-store',
      headers: {'Content-Type': file.type, 'X-Filename': encodeURIComponent(file.name)},
      body: file,
    });
    let body = null;
    try { body = await res.json(); } catch {}
    if (!res.ok) throw new Error(body?.error || `${res.status} ${res.statusText}`);
    await refreshTargetStatus();
    els.targetFile.value = '';
  } catch (err) {
    els.targetStatus.textContent = `Target upload failed: ${err.message}`;
    els.targetStatus.className = 'target-status error';
  } finally {
    els.uploadTargetBtn.disabled = false;
  }
}


function updateSearchMode() {
  const large = els.searchMode?.value === 'large';
  els.standardRadiusLabel?.classList.toggle('hidden', large);
  els.largeRadiusLabel?.classList.toggle('hidden', !large);
  els.standardSearchActions?.classList.toggle('hidden', large);
  els.largeAreaPanel?.classList.toggle('hidden', !large);
  if (large && largeAreaJobId) pollLargeAreaJob();
}

function largeAreaParamsFromForm() {
  const p = paramsFromForm();
  return {
    name: els.largeJobName.value.trim(),
    centerLat: p.centerLat,
    centerLng: p.centerLng,
    overallRadiusM: Number(els.largeRadiusM.value),
    imageType: p.imageType,
    startCapturedAt: p.startCapturedAt,
    endCapturedAt: p.endCapturedAt,
    maxApiImages: p.maxApiImages,
  };
}

function setLargeStatus(text, kind='') {
  els.largeStatus.textContent = text;
  els.largeStatus.className = `status ${kind}`.trim();
}

async function startLargeAreaSearch() {
  const p = largeAreaParamsFromForm();
  if (p.centerLat == null || p.centerLng == null || !Number.isFinite(p.centerLat) || !Number.isFinite(p.centerLng)) {
    setLargeStatus('Enter valid center latitude and longitude.','error'); return;
  }
  if (p.centerLat < -90 || p.centerLat > 90 || p.centerLng < -180 || p.centerLng > 180) {
    setLargeStatus('Center latitude/longitude is outside the valid geographic range.','error'); return;
  }
  if (!Number.isFinite(p.overallRadiusM) || p.overallRadiusM <= 1500 || p.overallRadiusM > 50000) {
    setLargeStatus('A radius of 1,500 m or less can be searched directly with Standard Search. Large Area Search accepts 1,501–50,000 m.','error'); return;
  }
  if (!accessToken) { setLargeStatus('Configure MAPILLARY_ACCESS_TOKEN or config.json first.','error'); return; }
  if (!confirm(`Start a checkpointed Large Area Search with an overall radius of ${Math.round(p.overallRadiusM).toLocaleString()} m? The search may take a long time and will make many Mapillary API requests.`)) return;
  els.largeStartBtn.disabled = true;
  setLargeStatus('Generating cells and starting Large Area Search…');
  try {
    const job = await api('/api/large-area/jobs/start',{method:'POST',body:JSON.stringify(p)});
    largeAreaJobId = job.id;
    await refreshLargeAreaJobs(job.id);
    renderLargeAreaJob(job);
    startLargeAreaPolling();
  } catch (err) {
    setLargeStatus(err.message,'error');
  } finally {
    els.largeStartBtn.disabled = false;
  }
}

function startLargeAreaPolling() {
  if (largeAreaPollTimer) clearInterval(largeAreaPollTimer);
  if (!largeAreaJobId) return;
  largeAreaPollTimer = setInterval(pollLargeAreaJob, 1500);
}

function stopLargeAreaPolling() {
  if (largeAreaPollTimer) { clearInterval(largeAreaPollTimer); largeAreaPollTimer = null; }
}

async function refreshLargeAreaJobs(selectId=null) {
  try {
    const data = await api('/api/large-area/jobs');
    const jobs = data.jobs || [];
    largeAreaJobsCache = new Map(jobs.map(j => [String(j.id), j]));
    const prior = selectId || largeAreaJobId || els.largeJobs?.value || '';
    els.largeJobs.innerHTML = jobs.length ? '<option value="">Select Large Area Search job…</option>' : '<option value="">No Large Area Search jobs</option>';
    for (const j of jobs) {
      const c=j.cellCounts||{};
      const opt=document.createElement('option');
      opt.value=j.id;
      opt.textContent=`${j.name} — ${j.status} — ${(j.uniqueImages||0).toLocaleString()} unique — ${c.complete||0}/${c.total||0} cells`;
      els.largeJobs.appendChild(opt);
    }
    if (prior && largeAreaJobsCache.has(String(prior))) {
      els.largeJobs.value=String(prior);
      largeAreaJobId=String(prior);
    }
    updateLargeAreaButtons(largeAreaJobsCache.get(String(largeAreaJobId)) || null);
    return jobs;
  } catch (err) {
    console.warn('Large Area Search list:', err);
    return [];
  }
}

async function selectLargeAreaJob() {
  largeAreaJobId = els.largeJobs.value || null;
  stopLargeAreaPolling();
  if (!largeAreaJobId) {
    els.largeProgress.value=0; els.largeProgress.max=1;
    els.largeProgressText.textContent='No job selected.';
    els.largeStats.innerHTML=''; els.largeCellTable.innerHTML=''; els.largeCellTable.classList.add('hidden');
    updateLargeAreaButtons(null); return;
  }
  await pollLargeAreaJob();
}

async function pollLargeAreaJob() {
  if (!largeAreaJobId) return;
  try {
    const job = await api(`/api/large-area/jobs/${encodeURIComponent(largeAreaJobId)}`);
    renderLargeAreaJob(job);
    const active = ['pending','running','pausing'].includes(job.status);
    if (active) startLargeAreaPolling(); else stopLargeAreaPolling();
    await refreshLargeAreaJobs(job.id);
  } catch (err) {
    setLargeStatus(`Large Area Search status error: ${err.message}`,'error');
    stopLargeAreaPolling();
  }
}

function updateLargeAreaButtons(job) {
  const status=job?.status||'';
  const c=job?.cellCounts||{};
  const active=['pending','running','pausing'].includes(status);
  els.largePauseBtn.disabled=!job || !['pending','running'].includes(status);
  els.largeCancelBtn.disabled=!job || !active;
  els.largeResumeBtn.disabled=!job || !['paused','cancelled','error'].includes(status);
  els.largeRetryBtn.disabled=!job || active || !(Number(c.failed||0)>0);
  els.largeLoadBtn.disabled=!job || !(Number(job.uniqueImages||0)>0);
  els.largeDeleteBtn.disabled=!job || active;
}

function renderLargeAreaJob(job) {
  if (!job) return;
  largeAreaJobId=String(job.id);
  if (els.largeJobs && [...els.largeJobs.options].some(o=>o.value===String(job.id))) els.largeJobs.value=String(job.id);
  const c=job.cellCounts||{};
  const total=Number(c.total||0);
  const finished=Number(c.complete||0)+Number(c.failed||0);
  els.largeProgress.max=Math.max(1,total);
  els.largeProgress.value=Math.min(finished,total);
  const current=(job.cells||[]).find(x=>x.status==='running');
  const currentText=current ? ` · current ${current.cell_id}` : '';
  els.largeProgressText.textContent=`${finished.toLocaleString()} / ${total.toLocaleString()} cells${currentText}`;
  const kind=['complete'].includes(job.status) ? 'ok' : (['error','complete_with_errors'].includes(job.status) ? 'error' : '');
  const statusLabels={pending:'Pending',running:'Running',pausing:'Pausing after current cell',paused:'Paused',cancelled:'Cancelled',complete:'Complete',complete_with_errors:'Complete with failed cells',error:'Error'};
  setLargeStatus(`${statusLabels[job.status]||job.status}${job.error ? ` — ${job.error}` : ''}`,kind);
  els.largeStats.innerHTML=`
    <div><span>Cells</span><strong>${Number(c.complete||0).toLocaleString()} complete / ${total.toLocaleString()}</strong></div>
    <div><span>Failed</span><strong>${Number(c.failed||0).toLocaleString()}</strong></div>
    <div><span>Raw cell results</span><strong>${Number(job.rawImages||0).toLocaleString()}</strong></div>
    <div><span>Unique images</span><strong>${Number(job.uniqueImages||0).toLocaleString()}</strong></div>
    <div><span>Duplicates removed</span><strong>${Number(job.duplicatesRemoved||0).toLocaleString()}</strong></div>
    <div><span>API images examined</span><strong>${Number(job.apiImagesSeen||0).toLocaleString()}</strong></div>
    <div><span>API requests</span><strong>${Number(job.apiRequestCount||0).toLocaleString()}</strong></div>
    <div><span>Cells hitting cap</span><strong>${Number(job.capCells||0).toLocaleString()}</strong></div>`;
  renderLargeAreaCells(job.cells||[]);
  updateLargeAreaButtons(job);
}

function renderLargeAreaCells(cells) {
  if (!cells.length) {
    els.largeCellTable.innerHTML='';
    els.largeCellTable.classList.add('hidden');
    lastLargeAreaActiveCellId = null;
    return;
  }
  const active = cells.find(x=>x.status==='running');
  const activeId = active ? String(active.cell_id) : null;
  const rows = [...cells].sort((a,b)=>Number(a.cell_number)-Number(b.cell_number)).map(x=>{
    const cls = x.status === 'running' ? ' class="active-cell"' : (x.status === 'failed' ? ' class="failed-cell"' : '');
    return `<tr${cls} data-cell-id="${escapeHtml(x.cell_id)}"><td>${escapeHtml(x.cell_id)}</td><td>${Number(x.lat).toFixed(6)}, ${Number(x.lng).toFixed(6)}</td><td>${escapeHtml(x.status)}</td><td>${Number(x.raw_image_count||0).toLocaleString()}</td><td>${Number(x.attempts||0)}</td><td>${x.cap_reached ? 'yes' : ''}</td><td>${escapeHtml(x.error||'')}</td></tr>`;
  }).join('');
  els.largeCellTable.innerHTML=`<table><thead><tr><th>Cell</th><th>Center</th><th>Status</th><th>Images</th><th>Attempts</th><th>Cap</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table>`;
  els.largeCellTable.classList.remove('hidden');

  // Auto-scroll only when the active cell changes. This keeps the running cell
  // visible without repeatedly fighting a user who manually scrolls the list.
  if (activeId && activeId !== lastLargeAreaActiveCellId) {
    const row = [...els.largeCellTable.querySelectorAll('tr[data-cell-id]')].find(r => r.dataset.cellId === activeId);
    if (row) {
      const rowTop = row.offsetTop;
      const rowBottom = rowTop + row.offsetHeight;
      const viewTop = els.largeCellTable.scrollTop;
      const viewBottom = viewTop + els.largeCellTable.clientHeight;
      if (rowTop < viewTop || rowBottom > viewBottom) {
        els.largeCellTable.scrollTop = Math.max(0, rowTop - Math.floor(els.largeCellTable.clientHeight / 2));
      }
    }
  }
  lastLargeAreaActiveCellId = activeId;
}

async function largeAreaAction(action, message) {
  if (!largeAreaJobId) return;
  try {
    const result=await api(`/api/large-area/jobs/${encodeURIComponent(largeAreaJobId)}/${action}`,{method:'POST',body:'{}'});
    if (message) setLargeStatus(message);
    if (result?.id) renderLargeAreaJob(result);
    startLargeAreaPolling();
    await pollLargeAreaJob();
  } catch (err) { setLargeStatus(err.message,'error'); }
}

async function pauseLargeAreaSearch() { await largeAreaAction('pause','Pause requested; the current cell will finish before the job pauses.'); }
async function cancelLargeAreaSearch() {
  if (!largeAreaJobId || !confirm('Cancel this Large Area Search? Completed cell results will be preserved and the job can be resumed later.')) return;
  await largeAreaAction('cancel','Cancellation requested; the current cell will finish first.');
}
async function resumeLargeAreaSearch() { await largeAreaAction('resume','Resuming Large Area Search…'); }
async function retryLargeAreaFailed() { await largeAreaAction('retry-failed','Retrying failed cells…'); }

async function loadLargeAreaResults() {
  if (!largeAreaJobId) return;
  setLargeStatus('Loading combined deduplicated candidate set…');
  try {
    const job=await api(`/api/large-area/jobs/${encodeURIComponent(largeAreaJobId)}/results`);
    currentImages=job.images||[];
    originalDistanceOrder=currentImages.map(x=>String(x.id));
    currentOrderMode='distance'; updateOrderStatus();
    const p=job.params||{};
    currentRunMeta={
      name:job.name||'Large Area Search', savedAt:null, status:`large_area_${job.status}`,
      params:{centerLat:p.centerLat,centerLng:p.centerLng,radiusM:p.overallRadiusM,imageType:p.imageType||'all',startCapturedAt:p.startCapturedAt||'',endCapturedAt:p.endCapturedAt||'',maxApiImages:p.maxApiImages||20000,searchMode:'large',largeAreaJobId:job.id},
      apiImagesSeen:job.apiImagesSeen||0, sourceLargeAreaJobId:job.id
    };
    metadataCache.clear(); resetAiPanelForRun(); updateAiImageTypeControls();
    els.runName.value=job.name||'';
    els.searchMode.value='large'; els.largeRadiusM.value=p.overallRadiusM||''; updateSearchMode();
    els.centerLat.value=p.centerLat??''; els.centerLng.value=p.centerLng??'';
    els.imageType.value=p.imageType||'all'; els.startCapturedAt.value=p.startCapturedAt||''; els.endCapturedAt.value=p.endCapturedAt||''; els.maxApiImages.value=p.maxApiImages||20000;
    setStatus(`Loaded ${currentImages.length.toLocaleString()} unique images from Large Area Search "${job.name}".`,'ok');
    setLargeStatus(`Loaded ${currentImages.length.toLocaleString()} combined unique images into the reviewer.`,'ok');
    if (currentImages.length) showIndex(0); else clearViewer('Large Area Search currently has no discovered images.');
    updateButtons();
  } catch (err) { setLargeStatus(err.message,'error'); }
}

async function deleteLargeAreaJob() {
  if (!largeAreaJobId) return;
  const job=largeAreaJobsCache.get(String(largeAreaJobId));
  if (!confirm(`Delete Large Area Search "${job?.name||largeAreaJobId}" and all of its checkpointed cells/results?`)) return;
  try {
    await api(`/api/large-area/jobs/${encodeURIComponent(largeAreaJobId)}`,{method:'DELETE'});
    largeAreaJobId=null; stopLargeAreaPolling();
    await refreshLargeAreaJobs();
    els.largeProgress.value=0; els.largeProgress.max=1; els.largeProgressText.textContent='No job selected.';
    els.largeStats.innerHTML=''; els.largeCellTable.innerHTML=''; els.largeCellTable.classList.add('hidden');
    setLargeStatus('Large Area Search job deleted.','ok');
  } catch (err) { setLargeStatus(err.message,'error'); }
}

async function discover() {
  const p = paramsFromForm();
  if (p.centerLat == null || p.centerLng == null || !Number.isFinite(p.centerLat) || !Number.isFinite(p.centerLng)) {
    setStatus('Enter valid center latitude and center longitude before starting a run.','error');
    return;
  }
  if (p.centerLat < -90 || p.centerLat > 90 || p.centerLng < -180 || p.centerLng > 180) {
    setStatus('Center latitude/longitude is outside the valid geographic range.','error');
    return;
  }
  if (!Number.isFinite(p.radiusM) || p.radiusM <= 0 || p.radiusM > 1500) {
    setStatus('Standard Search radius must be greater than 0 and no more than 1,500 meters. Use Large Area Search for larger areas.','error'); return;
  }
  if (!accessToken) { setStatus('Configure MAPILLARY_ACCESS_TOKEN or config.json first.','error'); return; }
  els.discoverBtn.disabled = true;
  setStatus('Querying Mapillary…');
  try {
    const result = await api('/api/discover', {method:'POST', body:JSON.stringify(p)});
    currentImages = result.images || [];
    originalDistanceOrder = currentImages.map(x => String(x.id));
    currentOrderMode = 'distance';
    updateOrderStatus();
    resetAiPanelForRun();
    currentRunMeta = {
      id: crypto.randomUUID(), name: '', savedAt: null, status: result.status,
      params: p, apiImagesSeen: result.apiImagesSeen || 0, pageCount: result.pageCount || 0
    };
    metadataCache.clear();
    updateAiImageTypeControls();
    const splitInfo = result.splitCount ? `; adaptive search split ${result.splitCount} time(s) across ${result.apiRequestCount || result.pageCount} API request(s)` : '';
    const msg = `${currentImages.length.toLocaleString()} images inside circle; ${result.apiImagesSeen.toLocaleString()} unique API images examined across ${result.pageCount} successful page(s)${splitInfo}` + (result.status === 'cap_reached' ? ' — CAP REACHED' : '.');
    setStatus(msg, result.status === 'cap_reached' ? 'error' : 'ok');
    if (currentImages.length) showIndex(0); else clearViewer('No Mapillary images were found inside this circle.');
  } catch (err) {
    setStatus(err.message,'error');
    clearViewer('Discovery failed.');
  } finally {
    els.discoverBtn.disabled = false;
    updateButtons();
  }
}

function clearViewer(message='No image loaded.') {
  currentIndex = -1;
  els.candidateCounter.textContent = message;
  els.metadata.innerHTML = `<div class="empty">${escapeHtml(message)}</div>`;
  els.thumbImage.style.display = 'none';
  els.thumbStatus.style.display = 'block'; els.thumbStatus.textContent = message;
  if (viewer) { try { viewer.remove(); } catch {} viewer = null; }
  els.mlyViewer.innerHTML = '';
  updateButtons();
}

function showIndex(index) {
  if (!currentImages.length) return;
  index = Math.max(0, Math.min(index, currentImages.length - 1));
  currentIndex = index;
  els.jumpInput.value = String(index + 1);
  renderCurrent();
  updateButtons();
}

async function renderCurrent() {
  const im = currentImage();
  if (!im) return;
  els.candidateCounter.textContent = `Image ${(currentIndex + 1).toLocaleString()} of ${currentImages.length.toLocaleString()}` + (im.aiScore == null ? '' : ` · AI ${Number(im.aiScore).toFixed(4)}`);
  renderMetadata(im);
  updateShortlistButton();
  if (els.viewerMode.value === 'thumbnail') {
    els.mlyViewer.classList.add('hidden');
    els.thumbViewer.classList.remove('hidden');
    await renderThumbnail(im);
  } else {
    els.thumbViewer.classList.add('hidden');
    els.mlyViewer.classList.remove('hidden');
    renderMapillaryJS(im);
  }
}

function renderMapillaryJS(im) {
  if (!accessToken || typeof mapillary === 'undefined') {
    els.mlyViewer.innerHTML = '<div class="empty">MapillaryJS or access token is unavailable.</div>';
    return;
  }
  try {
    if (!viewer) {
      const { Viewer } = mapillary;
      viewer = new Viewer({
        accessToken,
        container: 'mlyViewer',
        imageId: String(im.id),
        component: { cover: false, sequence: { visible: true } },
      });
      viewer.on('error', ev => console.warn('MapillaryJS:', ev));
    } else {
      viewer.moveTo(String(im.id)).catch(err => console.warn('MapillaryJS moveTo:', err));
      setTimeout(() => { try { viewer.resize(); } catch {} }, 30);
    }
  } catch (err) {
    console.error(err);
    els.mlyViewer.innerHTML = `<div class="empty">MapillaryJS error: ${escapeHtml(err.message)}</div>`;
    viewer = null;
  }
}

async function freshMetadata(im) {
  if (metadataCache.has(im.id)) return metadataCache.get(im.id);
  const data = await api(`/api/image/${encodeURIComponent(im.id)}`);
  metadataCache.set(im.id, data);
  return data;
}

async function renderThumbnail(im) {
  els.thumbImage.style.display = 'none';
  els.thumbStatus.style.display = 'block';
  els.thumbStatus.textContent = 'Loading fresh Mapillary thumbnail URL…';
  try {
    const data = await freshMetadata(im);
    const url = data.thumb_2048_url || data.thumb_1024_url;
    if (!url) throw new Error('No thumbnail URL returned for this image.');
    els.thumbImage.onload = () => { els.thumbStatus.style.display='none'; els.thumbImage.style.display='block'; };
    els.thumbImage.onerror = () => { els.thumbImage.style.display='none'; els.thumbStatus.style.display='block'; els.thumbStatus.textContent='Thumbnail failed to load.'; };
    els.thumbImage.src = url;
    renderMetadata(im, data);
  } catch (err) {
    els.thumbStatus.textContent = err.message;
  }
}

function renderMetadata(im, fresh=null) {
  let creator = fresh?.creator?.username || 'load thumbnail to fetch';
  let sequence = im.sequenceId || fresh?.sequence || 'n/a';
  const pairs = [
    ['Image ID', im.id], ['Distance to center', `${num(im.distanceM,1)} m`],
    ['Latitude', Number(im.lat).toFixed(7)], ['Longitude', Number(im.lng).toFixed(7)],
    ['Captured at', formatDate(im.capturedAt ?? fresh?.captured_at)], ['Compass angle', im.compassAngle == null ? 'n/a' : `${num(im.compassAngle,1)}°`],
    ['Panorama', yesno(im.isPano)], ['Camera type', im.cameraType || fresh?.camera_type || 'n/a'],
    ['Sequence ID', sequence], ['Creator', creator],
    ['AI similarity', im.aiScore == null ? 'not scored' : Number(im.aiScore).toFixed(6)], ['AI best crop', im.aiBestCrop || 'n/a'],
    ['AI model', im.aiModel || 'n/a'], ['AI mode', im.aiMode || 'n/a'],
    ['AI image type', im.aiImageType || 'n/a'],
  ];
  els.metadata.innerHTML = pairs.map(([k,v]) => `<div class="meta-key">${escapeHtml(k)}</div><div class="meta-val">${escapeHtml(String(v))}</div>`).join('');
}

function updateButtons() {
  const has = !!currentImage();
  els.prevBtn.disabled = !has || currentIndex <= 0;
  els.nextBtn.disabled = !has || currentIndex >= currentImages.length - 1;
  els.jumpBtn.disabled = !has; els.jumpInput.disabled = !has;
  els.shortlistBtn.disabled = !has; els.openMapillaryBtn.disabled = !has; els.copyIdBtn.disabled = !has;
  els.saveRunBtn.disabled = !currentImages.length;
  els.exportRunBtn.disabled = !currentImages.length;
  const aiActive = !!aiJobId;
  const selectedType = els.aiImageType?.value || 'all';
  const selectedCount = Number(aiImageTypeCounts[selectedType] || 0);
  els.aiRankBtn.disabled = !currentImages.length || !selectedCount || aiActive || !targetConfigured;
  els.aiCancelBtn.disabled = !aiActive;
  els.aiSortBtn.disabled = !currentImages.some(x => x.aiScore != null);
  els.distanceSortBtn.disabled = !currentImages.length;
  if (aiActive) {
    els.discoverBtn.disabled = true;
    els.loadRunBtn.disabled = true;
    els.deleteRunBtn.disabled = true;
    els.loadShortlistBtn.disabled = true;
    els.saveRunBtn.disabled = true;
  } else {
    els.discoverBtn.disabled = false;
    els.loadRunBtn.disabled = false;
    els.deleteRunBtn.disabled = false;
    els.loadShortlistBtn.disabled = false;
  }
}

function updateShortlistButton() {
  const im = currentImage();
  const id = im ? String(im.id) : '';
  els.shortlistBtn.textContent = im && shortlistIds.has(id) ? 'Remove from shortlist' : 'Add to shortlist';
}

async function toggleShortlist() {
  const im = currentImage(); if (!im) return;
  try {
    const id = String(im.id);
    if (shortlistIds.has(id)) {
      await api('/api/shortlist/remove',{method:'POST',body:JSON.stringify(im)});
      shortlistIds.delete(id);
    } else {
      await api('/api/shortlist/add',{method:'POST',body:JSON.stringify(im)});
      shortlistIds.add(id);
    }
    await refreshShortlist(false);
    updateShortlistButton();
    syncAiTopShortlistButtons();
  } catch (err) { setStatus(err.message,'error'); }
}

async function refreshShortlist(resetSet=true) {
  try {
    const data = await api('/api/shortlist');
    shortlistIds = new Set((data.items||[]).map(x=>String(x.id)));
    els.shortlistCount.textContent = String(shortlistIds.size);
    syncAiTopShortlistButtons();
    return data.items || [];
  } catch { return []; }
}

async function loadShortlist() {
  const items = await refreshShortlist();
  currentImages = items;
  originalDistanceOrder = [...currentImages].sort((a,b)=>Number(a.distanceM||0)-Number(b.distanceM||0)).map(x=>String(x.id));
  currentOrderMode = 'distance';
  updateOrderStatus();
  currentRunMeta = {id:crypto.randomUUID(),name:'Shortlist',savedAt:null,status:'shortlist',params:paramsFromForm(),apiImagesSeen:items.length};
  metadataCache.clear(); resetAiPanelForRun(); updateAiImageTypeControls(); showExistingAiSummary();
  if (items.length) { setStatus(`Loaded ${items.length} shortlisted image(s).`,'ok'); showIndex(0); }
  else clearViewer('Shortlist is empty.');
}

async function clearShortlist() {
  if (!shortlistIds.size) return;
  if (!confirm(`Clear all ${shortlistIds.size} shortlisted image(s)?`)) return;
  try {
    await api('/api/shortlist/clear',{method:'POST',body:'{}'});
    shortlistIds.clear(); els.shortlistCount.textContent='0'; updateShortlistButton(); syncAiTopShortlistButtons();
    setStatus('Shortlist cleared.','ok');
  } catch (err) { setStatus(err.message,'error'); }
}


function imageMatchesAiType(im, imageType) {
  if (imageType === 'pano') return im?.isPano === true;
  if (imageType === 'flat') return im?.isPano === false;
  return true;
}

function calculateAiImageTypeCounts() {
  let pano=0, flat=0, unknown=0;
  for (const im of currentImages) {
    if (im?.isPano === true) pano++;
    else if (im?.isPano === false) flat++;
    else unknown++;
  }
  aiImageTypeCounts = {all:currentImages.length,pano,flat,unknown};
  return aiImageTypeCounts;
}

function updateAiImageTypeControls(recalculate=true) {
  if (!els.aiImageType) return;
  const counts = recalculate ? calculateAiImageTypeCounts() : aiImageTypeCounts;
  const labels = {
    all:`Both panorama and flat images — ${counts.all.toLocaleString()}`,
    pano:`Panoramas only — ${counts.pano.toLocaleString()}`,
    flat:`Flat images only — ${counts.flat.toLocaleString()}`,
  };
  for (const opt of els.aiImageType.options) {
    opt.textContent = labels[opt.value] || opt.textContent;
    opt.disabled = (opt.value === 'pano' && counts.pano === 0) || (opt.value === 'flat' && counts.flat === 0);
  }
  if (els.aiImageType.selectedOptions[0]?.disabled) els.aiImageType.value='all';
  const selected = els.aiImageType.value || 'all';
  const selectedCount = Number(counts[selected] || 0);
  const unknownText = counts.unknown ? ` · ${counts.unknown.toLocaleString()} unknown-type image(s) included only in Both` : '';
  els.aiImageTypeSummary.textContent = `Current candidate set: ${counts.all.toLocaleString()} total · ${counts.pano.toLocaleString()} panorama · ${counts.flat.toLocaleString()} flat${unknownText}. AI selection: ${selectedCount.toLocaleString()} image(s).`;
}

function aiCandidateImages(imageType=els.aiImageType?.value || 'all') {
  return currentImages.filter(im => imageMatchesAiType(im, imageType));
}

function formatTimestamp(value) {
  if (!value) return 'n/a';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

function elapsedFromStartedAt(startedAt) {
  if (!startedAt) return null;
  const ms = Date.now() - new Date(startedAt).getTime();
  return Number.isFinite(ms) ? Math.max(0, ms / 1000) : null;
}

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  const units = ['KB','MB','GB','TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 ? 1 : 2)} ${units[i]}`;
}

function formatDuration(sec) {
  if (sec == null || !Number.isFinite(Number(sec))) return 'n/a';
  let s = Math.max(0, Math.round(Number(sec)));
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function aiSettingsFromForm() {
  return {
    mode: els.aiMode.value,
    imageType: els.aiImageType.value || 'all',
    thumbSize: Number(els.aiThumbSize.value || 1024),
    device: els.aiDevice.value,
    maxImages: Math.max(0, Number(els.aiMaxImages.value || 0)),
    keepCache: !!els.aiKeepCache.checked,
  };
}

function resetAiPanelForRun() {
  els.aiProgress.value = 0;
  els.aiProgress.max = 1;
  els.aiProgressText.textContent = 'Ready.';
  els.aiTop.innerHTML = '';
  els.aiTop.classList.add('hidden');
  lastAiScoredIds = new Set();
  lastAiImageType = 'all';
  lastAiJobStats = null;
}

function updateAiDeviceOptions(info) {
  const details = new Map((info?.deviceDetails || []).map(d => [d.key, d]));
  for (const opt of els.aiDevice.options) {
    if (opt.value === 'auto') {
      const pref = info?.preferredDeviceLabel || 'best available accelerator';
      opt.textContent = `Auto — ${pref}`;
      opt.disabled = false;
      continue;
    }
    const d = details.get(opt.value);
    if (d) {
      opt.textContent = d.label;
      opt.disabled = false;
    } else {
      const base = opt.dataset.baseLabel || opt.textContent.replace(/ \(unavailable\)$/,'');
      opt.dataset.baseLabel = base;
      opt.textContent = `${base} (unavailable)`;
      opt.disabled = true;
    }
  }
  if (els.aiDevice.selectedOptions[0]?.disabled) els.aiDevice.value = 'auto';
}

async function refreshAiEnvironment(showMessage=true) {
  try {
    const info = await api('/api/ai/status');
    if (info.ready) {
      updateAiDeviceOptions(info);
      const labels = (info.deviceDetails || []).map(d => d.label);
      els.aiReadyBadge.textContent = `AI ready · ${info.preferredDeviceLabel || labels[0] || 'CPU'}`;
      els.aiReadyBadge.className = 'badge';
      const cache = info.cache || {};
      const hardware = labels.length ? ` Available: ${labels.join('; ')}.` : '';
      if (showMessage || !els.aiStatus.textContent) {
        els.aiStatus.textContent = `${info.message}${hardware} Cache: ${(cache.files||0).toLocaleString()} file(s), ${formatBytes(cache.bytes||0)}.`;
        els.aiStatus.className = 'status ok';
      } else if (els.aiStatus.textContent.includes('Install the optional')) {
        els.aiStatus.textContent = `AI ready.${hardware} Cache: ${(cache.files||0).toLocaleString()} file(s), ${formatBytes(cache.bytes||0)}.`;
        els.aiStatus.className = 'status ok';
      }
    } else {
      els.aiReadyBadge.textContent = 'AI dependencies missing';
      els.aiReadyBadge.className = 'badge bad';
      els.aiStatus.textContent = `${info.message || 'AI dependencies are not installed.'} Run install_ai.ps1 or: python -m pip install -r requirements-ai.txt`;
      els.aiStatus.className = 'status error';
    }
    return info;
  } catch (err) {
    els.aiReadyBadge.textContent = 'AI check failed';
    els.aiReadyBadge.className = 'badge bad';
    if (showMessage) {
      els.aiStatus.textContent = err.message;
      els.aiStatus.className = 'status error';
    }
    return null;
  }
}

async function startAiRanking() {
  if (!currentImages.length || aiJobId) return;
  if (!targetConfigured && !(await refreshTargetStatus())) {
    els.aiStatus.textContent = 'Choose and upload a target image before starting AI ranking.';
    els.aiStatus.className = 'status error';
    return;
  }
  const env = await refreshAiEnvironment(false);
  if (!env?.ready) {
    els.aiStatus.textContent = 'AI dependencies are not ready. Run install_ai.ps1, restart serve.py, then try again.';
    els.aiStatus.className = 'status error';
    return;
  }
  const settings = aiSettingsFromForm();
  const selectedImages = aiCandidateImages(settings.imageType);
  if (!selectedImages.length) {
    const label = settings.imageType === 'pano' ? 'panorama' : (settings.imageType === 'flat' ? 'flat' : 'selected');
    els.aiStatus.textContent = `No ${label} images are available in the current candidate set. Choose another AI image type.`;
    els.aiStatus.className = 'status error';
    updateButtons();
    return;
  }
  const count = settings.maxImages > 0 ? Math.min(settings.maxImages, selectedImages.length) : selectedImages.length;
  const modeLabel = settings.mode === 'thorough' ? 'Thorough' : 'Fast';
  const imageTypeLabel = settings.imageType === 'pano' ? 'panorama' : (settings.imageType === 'flat' ? 'flat' : 'all available');
  if (count > 2000 && settings.mode === 'thorough') {
    const ok = confirm(`Thorough mode will score ${count.toLocaleString()} ${imageTypeLabel} image(s) using many crops per image and can take a long time. Continue?`);
    if (!ok) return;
  }
  try {
    els.aiStatus.textContent = `Starting ${modeLabel} DINOv2 ranking for ${count.toLocaleString()} ${imageTypeLabel} image(s)…`;
    els.aiStatus.className = 'status';
    els.aiProgress.value = 0; els.aiProgress.max = Math.max(1, count);
    els.aiProgressText.textContent = 'Starting…';
    const response = await api('/api/ai/start', {
      method:'POST',
      body:JSON.stringify({images:selectedImages, settings})
    });
    aiJobId = response.id;
    aiJobRunId = currentRunMeta?.id || null;
    lastAiImageType = settings.imageType;
    updateButtons();
    if (aiPollTimer) clearInterval(aiPollTimer);
    await pollAiJob();
    aiPollTimer = setInterval(pollAiJob, 1200);
  } catch (err) {
    aiJobId = null; aiJobRunId = null;
    els.aiStatus.textContent = err.message;
    els.aiStatus.className = 'status error';
    updateButtons();
  }
}

async function pollAiJob() {
  if (!aiJobId) return;
  try {
    const job = await api(`/api/ai/jobs/${encodeURIComponent(aiJobId)}`);
    renderAiJob(job);
    if (['complete','cancelled','error'].includes(job.status)) {
      if (aiPollTimer) { clearInterval(aiPollTimer); aiPollTimer = null; }
      const sameRun = !aiJobRunId || aiJobRunId === currentRunMeta?.id;
      if (job.result && sameRun) applyAiResult(job.result, job);
      if (job.status === 'error') {
        const elapsed = job.elapsedSec ?? job.progress?.elapsedSec ?? elapsedFromStartedAt(job.startedAt);
        els.aiStatus.textContent = `AI ranking failed: ${job.error || 'unknown error'} · Started ${formatTimestamp(job.startedAt || job.createdAt)} · Ended ${formatTimestamp(job.finishedAt)} · Elapsed ${formatDuration(elapsed)}`;
        els.aiStatus.className = 'status error';
      } else if (!sameRun) {
        els.aiStatus.textContent = 'AI ranking finished, but a different run is now loaded, so the scores were not applied.';
        els.aiStatus.className = 'status error';
      }
      aiJobId = null; aiJobRunId = null;
      updateButtons();
      await refreshAiEnvironment(false);
    }
  } catch (err) {
    if (aiPollTimer) { clearInterval(aiPollTimer); aiPollTimer = null; }
    aiJobId = null; aiJobRunId = null;
    els.aiStatus.textContent = `AI status error: ${err.message}`;
    els.aiStatus.className = 'status error';
    updateButtons();
  }
}

function renderAiJob(job) {
  const p = job.progress || {};
  const total = Number(p.total || 1);
  const completed = Number(p.completed || 0);
  els.aiProgress.max = Math.max(1, total);
  els.aiProgress.value = Math.min(completed, total);
  const liveElapsed = p.elapsedSec != null ? Number(p.elapsedSec) : elapsedFromStartedAt(job.startedAt || job.createdAt);
  let extra = '';
  if (liveElapsed != null) extra += ` · elapsed ${formatDuration(liveElapsed)}`;
  if (p.imagesPerSec) extra += ` · ${Number(p.imagesPerSec).toFixed(2)} img/s`;
  if (p.etaSec != null) extra += ` · ETA ${formatDuration(p.etaSec)}`;
  if (p.failed) extra += ` · ${Number(p.failed).toLocaleString()} failed`;
  if (p.cacheHits) extra += ` · ${Number(p.cacheHits).toLocaleString()} cache hits`;
  els.aiProgressText.textContent = `${completed.toLocaleString()} / ${total.toLocaleString()}${extra}`;

  if (['queued','running'].includes(job.status)) {
    const mode = job.settings?.mode === 'thorough' ? 'Thorough' : 'Fast';
    const typeLabel = job.settings?.imageType === 'pano' ? 'panorama only' : (job.settings?.imageType === 'flat' ? 'flat only' : 'both image types');
    els.aiStatus.textContent = `${mode} AI ranking running (${typeLabel}): ${p.message || 'working'} · Started ${formatTimestamp(job.startedAt || job.createdAt)} · Elapsed ${formatDuration(liveElapsed)}`;
  } else {
    els.aiStatus.textContent = p.message || `AI job ${job.status}`;
  }
  els.aiStatus.className = job.status === 'error' ? 'status error' : (job.status === 'complete' ? 'status ok' : 'status');
  if (p.top?.length) renderAiTop(p.top, true);
}

function applyAiResult(result, job=null) {
  const scoreMap = new Map((result.scores || []).map(x => [String(x.id), x]));
  const scoredAt = job?.finishedAt || result.finishedAt || new Date().toISOString();
  const imageType = result.imageType || job?.settings?.imageType || lastAiImageType || 'all';
  lastAiScoredIds = new Set(scoreMap.keys());
  lastAiImageType = imageType;
  currentImages = currentImages.map(im => {
    const s = scoreMap.get(String(im.id));
    if (!s) return im;
    return {...im,
      aiScore:s.score,
      aiBestCrop:s.bestCrop,
      aiModel:s.model,
      aiMode:s.mode,
      aiImageType:s.imageType || imageType,
      aiThumbSize:s.thumbSize,
      aiScoredAt:scoredAt,
    };
  });
  const elapsed = Number(result.elapsedSec ?? job?.elapsedSec ?? 0);
  const rate = elapsed > 0 ? Number(result.scored || 0) / elapsed : null;
  lastAiJobStats = {
    mode: result.mode || job?.settings?.mode || '',
    imageType,
    startedAt: job?.startedAt || result.startedAt || '',
    finishedAt: job?.finishedAt || result.finishedAt || scoredAt,
    elapsedSec: elapsed,
    scored: Number(result.scored || 0),
    failed: Number(result.failed || 0),
    cacheHits: Number(result.cacheHits || 0),
    requested: Number(result.requested || 0),
    device: result.device || job?.progress?.device || '',
    imagesPerSec: rate,
  };
  if (currentRunMeta) {
    currentRunMeta.aiScoredAt = scoredAt;
    currentRunMeta.aiRunStats = lastAiJobStats;
  }
  for (const im of currentImages) {
    if (im.aiScore != null && shortlistIds.has(String(im.id)) && lastAiScoredIds.has(String(im.id))) {
      api('/api/shortlist/add',{method:'POST',body:JSON.stringify(im)}).catch(()=>{});
    }
  }
  renderAiTop(result.scores || [], false);
  if ((result.scores || []).length) sortByAiSimilarity();
  updateAiImageTypeControls();
  const typeLabel = imageType === 'pano' ? 'panorama only' : (imageType === 'flat' ? 'flat only' : 'both image types');
  const statusWord = result.status === 'complete' ? 'complete' : result.status;
  const rateText = rate ? ` · ${rate.toFixed(2)} img/s` : '';
  els.aiStatus.textContent = `AI ranking ${statusWord} (${typeLabel}): ${Number(result.scored||0).toLocaleString()} scored, ${Number(result.failed||0).toLocaleString()} failed, ${Number(result.cacheHits||0).toLocaleString()} cache hits · Started ${formatTimestamp(lastAiJobStats.startedAt)} · Ended ${formatTimestamp(lastAiJobStats.finishedAt)} · Elapsed ${formatDuration(elapsed)}${rateText} on ${lastAiJobStats.device || 'selected device'}. Press Save current run to persist these scores and run statistics.`;
  els.aiStatus.className = result.status === 'complete' ? 'status ok' : 'status';
}

function renderAiTop(scores, live=false) {
  if (!scores?.length) return;
  const rows = [...scores].sort((a,b)=>Number(b.score)-Number(a.score)).slice(0,10);
  els.aiTop.classList.remove('hidden');
  els.aiTop.innerHTML = `<table><thead><tr><th>Rank</th><th>Similarity</th><th>Image ID</th><th>Best crop</th><th>Actions</th></tr></thead><tbody>` +
    rows.map((s,i) => {
      const id = String(s.id);
      const shortlisted = shortlistIds.has(id);
      return `<tr><td>${i+1}</td><td class="ai-score">${Number(s.score).toFixed(6)}</td><td>${escapeHtml(id)}</td><td>${escapeHtml(s.bestCrop||'')}</td><td><div class="ai-actions"><button class="ai-go" data-id="${escapeHtml(id)}">Go</button><button class="ai-shortlist${shortlisted ? ' done' : ''}" data-id="${escapeHtml(id)}" ${shortlisted ? 'disabled' : ''}>${shortlisted ? 'Shortlisted' : 'Add to shortlist'}</button></div></td></tr>`;
    }).join('') +
    `</tbody></table>`;
  for (const btn of els.aiTop.querySelectorAll('.ai-go')) {
    btn.addEventListener('click', () => {
      const idx = currentImages.findIndex(x => String(x.id) === btn.dataset.id);
      if (idx >= 0) showIndex(idx);
    });
  }
  for (const btn of els.aiTop.querySelectorAll('.ai-shortlist')) {
    btn.addEventListener('click', async () => {
      const id = String(btn.dataset.id || '');
      const im = currentImages.find(x => String(x.id) === id);
      const score = rows.find(x => String(x.id) === id);
      if (!im || shortlistIds.has(id)) return;
      const payload = score ? {...im, aiScore:score.score, aiBestCrop:score.bestCrop || im.aiBestCrop, aiModel:score.model || im.aiModel, aiMode:score.mode || im.aiMode, aiImageType:score.imageType || lastAiImageType || im.aiImageType, aiThumbSize:score.thumbSize || im.aiThumbSize} : im;
      btn.disabled = true;
      try {
        await api('/api/shortlist/add',{method:'POST',body:JSON.stringify(payload)});
        shortlistIds.add(id);
        await refreshShortlist();
        updateShortlistButton();
        syncAiTopShortlistButtons();
      } catch (err) {
        btn.disabled = false;
        setStatus(err.message,'error');
      }
    });
  }
}

function syncAiTopShortlistButtons() {
  if (!els.aiTop) return;
  for (const btn of els.aiTop.querySelectorAll('.ai-shortlist')) {
    const isShortlisted = shortlistIds.has(String(btn.dataset.id || ''));
    btn.textContent = isShortlisted ? 'Shortlisted' : 'Add to shortlist';
    btn.disabled = isShortlisted;
    btn.classList.toggle('done', isShortlisted);
  }
}

async function cancelAiRanking() {
  if (!aiJobId) return;
  try {
    await api('/api/ai/cancel',{method:'POST',body:JSON.stringify({jobId:aiJobId})});
    els.aiStatus.textContent = 'Cancellation requested. The current image/crop batch will finish before the job stops.';
    els.aiStatus.className = 'status';
  } catch (err) {
    els.aiStatus.textContent = err.message;
    els.aiStatus.className = 'status error';
  }
}

function updateOrderStatus() {
  if (!els.orderStatus) return;
  els.orderStatus.textContent = currentOrderMode === 'ai' ? 'Order: AI similarity (highest first)' : 'Order: distance from center (nearest first)';
}

function sortByAiSimilarity() {
  if (!currentImages.length) return;
  const scoredIds = lastAiScoredIds.size ? lastAiScoredIds : new Set(currentImages.filter(x=>x.aiScore!=null).map(x=>String(x.id)));
  if (!scoredIds.size) {
    els.aiStatus.textContent = 'No AI scores are available for the current image set.';
    els.aiStatus.className = 'status error';
    return;
  }
  const positions = new Map(originalDistanceOrder.map((id,i) => [String(id), i]));
  currentImages = [...currentImages].sort((a,b) => {
    const as = scoredIds.has(String(a.id));
    const bs = scoredIds.has(String(b.id));
    if (as !== bs) return as ? -1 : 1;
    if (as && bs) {
      const av = a.aiScore == null ? -Infinity : Number(a.aiScore);
      const bv = b.aiScore == null ? -Infinity : Number(b.aiScore);
      if (bv !== av) return bv - av;
    }
    const ap = positions.has(String(a.id)) ? positions.get(String(a.id)) : Infinity;
    const bp = positions.has(String(b.id)) ? positions.get(String(b.id)) : Infinity;
    if (ap !== bp) return ap - bp;
    return 0;
  });
  currentOrderMode = 'ai';
  updateOrderStatus();
  showIndex(0);
  updateButtons();
}

function restoreDistanceOrder() {
  if (!currentImages.length) return;
  const positions = new Map(originalDistanceOrder.map((id,i) => [String(id), i]));
  currentImages = [...currentImages].sort((a,b) => {
    const ap = positions.has(String(a.id)) ? positions.get(String(a.id)) : Infinity;
    const bp = positions.has(String(b.id)) ? positions.get(String(b.id)) : Infinity;
    if (ap !== bp) return ap - bp;
    const ad = Number.isFinite(Number(a.distanceM)) ? Number(a.distanceM) : Infinity;
    const bd = Number.isFinite(Number(b.distanceM)) ? Number(b.distanceM) : Infinity;
    if (ad !== bd) return ad - bd;
    return Number(a.capturedAt || 0) - Number(b.capturedAt || 0);
  });
  currentOrderMode = 'distance';
  updateOrderStatus();
  showIndex(0);
  updateButtons();
}

async function clearAiCache() {
  if (aiJobId) return;
  if (!confirm('Delete locally cached Mapillary thumbnails in the ai_cache folder? Saved runs and AI scores will not be deleted.')) return;
  try {
    const result = await api('/api/ai/cache/clear',{method:'POST',body:'{}'});
    const removed = result.removed || {};
    els.aiStatus.textContent = `AI thumbnail cache cleared: ${(removed.files||0).toLocaleString()} file(s), ${formatBytes(removed.bytes||0)} removed.`;
    els.aiStatus.className = 'status ok';
    await refreshAiEnvironment(false);
  } catch (err) {
    els.aiStatus.textContent = err.message;
    els.aiStatus.className = 'status error';
  }
}

function showExistingAiSummary() {
  const allScores = currentImages.filter(x=>x.aiScore!=null).map(x=>({id:x.id,score:x.aiScore,bestCrop:x.aiBestCrop||'',model:x.aiModel||'',mode:x.aiMode||'',imageType:x.aiImageType||'',thumbSize:x.aiThumbSize||''}));
  if (!allScores.length) return;
  lastAiJobStats = currentRunMeta?.aiRunStats || null;
  const latestType = lastAiJobStats?.imageType || '';
  let scores = allScores;
  if (latestType && ['all','pano','flat'].includes(latestType)) {
    const matching = allScores.filter(x => x.imageType === latestType);
    if (matching.length) scores = matching;
    if (els.aiImageType && ![...els.aiImageType.options].find(o=>o.value===latestType)?.disabled) els.aiImageType.value=latestType;
    lastAiImageType = latestType;
  }
  lastAiScoredIds = new Set(scores.map(x=>String(x.id)));
  renderAiTop(scores,false);
  updateAiImageTypeControls(false);
  if (lastAiJobStats?.startedAt || lastAiJobStats?.finishedAt) {
    els.aiStatus.textContent = `Loaded ${allScores.length.toLocaleString()} saved AI score(s). Last AI run (${scores.length.toLocaleString()} image(s)): started ${formatTimestamp(lastAiJobStats.startedAt)} · ended ${formatTimestamp(lastAiJobStats.finishedAt)} · elapsed ${formatDuration(lastAiJobStats.elapsedSec)}.`;
  } else {
    els.aiStatus.textContent = `Loaded ${allScores.length.toLocaleString()} saved AI score(s).`;
  }
  els.aiStatus.className = 'status ok';
}

async function refreshRuns() {
  try {
    const data = await api('/api/runs');
    const runs = data.runs || [];
    els.savedRuns.innerHTML = runs.length ? '<option value="">Select saved run…</option>' : '<option value="">No saved runs</option>';
    for (const r of runs) {
      const opt = document.createElement('option');
      opt.value=r.id; opt.textContent=`${r.name} — ${r.imageCount} images — ${new Date(r.savedAt).toLocaleString()}`;
      els.savedRuns.appendChild(opt);
    }
  } catch (err) { console.warn(err); }
}

async function saveRun() {
  if (!currentImages.length) return;
  const name = els.runName.value.trim() || `Mapillary run ${new Date().toLocaleString()}`;
  const run = {
    id: currentRunMeta?.id || crypto.randomUUID(), name, savedAt:new Date().toISOString(),
    status:currentRunMeta?.status || 'saved', params:currentRunMeta?.params || paramsFromForm(),
    apiImagesSeen:currentRunMeta?.apiImagesSeen || currentImages.length, aiRunStats:currentRunMeta?.aiRunStats || lastAiJobStats || null, images:currentImages
  };
  try {
    const result=await api('/api/runs',{method:'POST',body:JSON.stringify(run)});
    currentRunMeta={...run,id:result.id};
    els.runName.value=name;
    await refreshRuns(); els.savedRuns.value=result.id;
    setStatus(`Saved "${name}" with ${result.imageCount} images.`,'ok');
  } catch(err){setStatus(err.message,'error');}
}

async function loadSelectedRun() {
  const id=els.savedRuns.value; if(!id) return;
  try {
    const run=await api(`/api/runs/${encodeURIComponent(id)}`);
    currentImages=run.images||[]; originalDistanceOrder=[...currentImages].sort((a,b)=>Number(a.distanceM||0)-Number(b.distanceM||0)).map(x=>String(x.id)); currentOrderMode='distance'; updateOrderStatus(); currentRunMeta=run; metadataCache.clear(); applyParams(run.params); els.runName.value=run.name||'';
    resetAiPanelForRun(); updateAiImageTypeControls(); showExistingAiSummary();
    setStatus(`Loaded "${run.name}" — ${currentImages.length} images.`,'ok');
    if(currentImages.length) showIndex(0); else clearViewer('Saved run contains no images.');
  } catch(err){setStatus(err.message,'error');}
}

async function deleteSelectedRun() {
  const id=els.savedRuns.value; if(!id) return;
  const label=els.savedRuns.options[els.savedRuns.selectedIndex]?.textContent||'this run';
  if(!confirm(`Delete ${label}?`)) return;
  try { await api(`/api/runs/${encodeURIComponent(id)}`,{method:'DELETE'}); await refreshRuns(); setStatus('Saved run deleted.','ok'); }
  catch(err){setStatus(err.message,'error');}
}

function csvEscape(v){const s=v==null?'':String(v);return /[",\n\r]/.test(s)?`"${s.replaceAll('"','""')}"`:s;}
function downloadText(name,text,type='text/csv;charset=utf-8'){const blob=new Blob([text],{type});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},100);}
function safeFileName(s){return String(s||'run').replace(/[^a-z0-9._-]+/gi,'_').replace(/^_+|_+$/g,'').slice(0,100)||'run';}

function buildCsv(images, meta, kind='run') {
  const p=meta?.params||paramsFromForm();
  const headers=['Run name','Saved at','Run status','Image number','Mapillary image ID','Latitude','Longitude','Distance from circle center (m)','Captured at (UTC)','Compass angle (deg)','Is panorama','Sequence ID','Camera type','AI similarity','AI best crop','AI model','AI mode','AI image type','AI thumbnail size','AI scored at','AI run started at','AI run finished at','AI elapsed seconds','AI scored count','AI failed count','AI cache hits','AI device','AI images/sec','Center latitude','Center longitude','Radius (meters)','Image type filter','Captured on/after','Captured on/before','Maximum API images','API images examined'];
  const rows=[headers];
  const stats=meta?.aiRunStats||{};
  images.forEach((im,i)=>rows.push([
    meta?.name||kind,meta?.savedAt||'',meta?.status||'',i+1,im.id,im.lat,im.lng,im.distanceM,formatDate(im.capturedAt),im.compassAngle??'',im.isPano==null?'':im.isPano,im.sequenceId||'',im.cameraType||'',
    im.aiScore??'',im.aiBestCrop||'',im.aiModel||'',im.aiMode||'',im.aiImageType||'',im.aiThumbSize??'',im.aiScoredAt||'',
    stats.startedAt||'',stats.finishedAt||'',stats.elapsedSec??'',stats.scored??'',stats.failed??'',stats.cacheHits??'',stats.device||'',stats.imagesPerSec??'',
    p.centerLat,p.centerLng,p.radiusM,p.imageType||'all',p.startCapturedAt||'',p.endCapturedAt||'',p.maxApiImages||'',meta?.apiImagesSeen||''
  ]));
  return rows.map(r=>r.map(csvEscape).join(',')).join('\r\n')+'\r\n';
}

function exportCurrentRun() {
  if(!currentImages.length)return;
  const name=currentRunMeta?.name||els.runName.value||'mapillary_run';
  downloadText(`${safeFileName(name)}.csv`,buildCsv(currentImages,currentRunMeta,'run'));
}

async function exportShortlist() {
  const items=await refreshShortlist();
  if(!items.length){setStatus('Shortlist is empty.','error');return;}
  const meta={name:'Mapillary shortlist',savedAt:new Date().toISOString(),status:'shortlist',params:paramsFromForm(),apiImagesSeen:items.length};
  downloadText('mapillary_shortlist.csv',buildCsv(items,meta,'shortlist'));
}

function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

document.addEventListener('DOMContentLoaded', init);

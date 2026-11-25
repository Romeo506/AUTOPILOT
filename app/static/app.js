'use strict';
/* AUTOPILOT UI. Plain JS, no CDN, works offline once loaded.
   All server data is rendered through h() (textContent) and never as HTML strings, so feed/brief text can never be interpreted as markup. */

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'hidden') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  const add = (k) => { if (k == null || k === false) return; if (Array.isArray(k)) k.forEach(add); else el.append(k.nodeType ? k : document.createTextNode(String(k))); };
  kids.forEach(add);
  return el;
}
const flat = (a) => a.flat(Infinity).filter(x => x != null && x !== false).map(x => x.nodeType ? x : document.createTextNode(String(x)));
const mount = (el, ...kids) => el.replaceChildren(...flat(kids));  // replaceChildren() does not flatten arrays by itself
async function api(path, o = {}) {
  const init = { method: o.method || (o.json || o.form ? 'POST' : 'GET'), headers: {} };
  if (o.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(o.json); }
  if (o.form) init.body = o.form;
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let data = null; try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) throw new Error((data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || `Request failed (${r.status})`);
  return data;
}
let toastTimer;
function toast(msg, kind = '') { const t = $('#toast'); t.textContent = msg; t.className = 'toast ' + kind; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => t.hidden = true, 4200); }
const fail = (e) => toast(e.message || String(e), 'bad');
async function act(btn, fn) { if (btn) btn.disabled = true; try { return await fn(); } catch (e) { fail(e); } finally { if (btn) btn.disabled = false; } }
function openSheet(title, ...body) { $('#sheetTitle').textContent = title; const b = $('#sheetBody'); mount(b, ...body); $('#sheet').hidden = false; }
function closeSheet() { $('#sheet').hidden = true; }
const fmtT = (s) => { s = Math.max(0, Math.round(s)); const hh = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60; return (hh ? hh + ':' + String(m).padStart(2, '0') : m) + ':' + String(ss).padStart(2, '0'); };
const fmtMoney = (n, c) => `${c} ${Number(n || 0).toFixed(2)}`;
const chipFor = (st) => h('span', { class: 'chip ' + ({ pass: 'ok', warn: 'warn', fail: 'bad', paid: 'ok', executed: 'ok', approved: 'ok', pending: 'warn', failed: 'bad', rejected: 'bad', unchecked: '' }[st] || '') }, st);
const statusChip = (st) => h('span', { class: 'chip ' + ({ delivered: 'ok', closed: 'ok', in_progress: 'warn', review: 'warn' }[st] || '') }, st.replace('_', ' '));
const field = (label, input) => h('label', {}, label, input);
const checkbox = (label, checked) => { const i = h('input', { type: 'checkbox', checked }); return [h('label', { class: 'check' }, i, label), i]; };
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const t = h('textarea', { value: text }); document.body.append(t); t.select(); document.execCommand('copy'); t.remove(); return Promise.resolve();
}
const state = { source: null, ranges: [], filter: 'all', apprTab: 'open', status: null };

// ------------------------------------------------------------------ jobs
let jobTimer = null;
const jobWaiters = new Map();
function watchJob(id, onDone) { jobWaiters.set(id, onDone); pollJobs(); }
async function pollJobs() {
  clearTimeout(jobTimer);
  let jobs = [];
  try { jobs = await api('/api/jobs'); } catch (_) { return; }
  const active = jobs.filter(j => j.status === 'queued' || j.status === 'running');
  const tray = $('#jobTray');
  tray.hidden = !active.length;
  tray.replaceChildren(...active.map(j => h('div', {}, h('div', { class: 'row between small' }, h('b', {}, j.kind), h('span', { class: 'muted' }, j.message || j.status)), h('div', { class: 'bar' }, h('i', { style: `width:${Math.round(j.progress * 100)}%` })))));
  for (const j of jobs) if (jobWaiters.has(j.id) && (j.status === 'done' || j.status === 'failed')) {
    const cb = jobWaiters.get(j.id); jobWaiters.delete(j.id);
    if (j.status === 'failed') toast(`${j.kind} failed: ${j.message}`, 'bad'); else toast(`${j.kind} finished`, 'ok');
    try { cb(j); } catch (e) { fail(e); }
  }
  if (active.length || jobWaiters.size) jobTimer = setTimeout(pollJobs, 1200);
}

// ------------------------------------------------------------------ shared renderers
function analysisView(a) {
  const out = [];
  out.push(h('div', { class: 'kv' }, ...a.summary.flatMap(l => { const [k, ...v] = l.split(': '); return [h('span', {}, k), h('span', {}, v.join(': '))]; })));
  if (a.red_flags.length) out.push(h('h3', {}, 'Warning signs'), ...a.red_flags.map(f => h('div', { class: 'banner ' + (f.severity === 'high' ? 'bad' : 'warn') }, f.text)));
  if (a.unsupported.length) out.push(h('h3', {}, 'Not supported by this app'), h('ul', { class: 'plain' }, a.unsupported.map(u => h('li', {}, u))));
  if (a.questions.length) out.push(h('h3', {}, 'Confirm with the client'), h('ul', { class: 'plain' }, a.questions.map(q => h('li', {}, q))));
  if (a.links.length) out.push(h('h3', {}, 'Links in the brief'), h('ul', { class: 'plain' }, a.links.map(l => h('li', {}, h('a', { href: l.url, target: '_blank', rel: 'noopener noreferrer' }, l.kind + ' ↗')))));
  out.push(h('p', { class: 'muted small' }, 'Rule-based reading of the text. It only reports what the text states. Always read the original brief.'));
  return out;
}
function qaView(qa) {
  if (!qa) return h('p', { class: 'muted' }, 'Not checked yet.');
  return h('div', {}, h('div', { class: 'row' }, chipFor(qa.status), h('span', { class: 'muted small' }, 'Caption presence is taken from render settings, not read from the picture.')),
    ...qa.checks.map(c => h('div', { class: 'row between', style: 'border-top:1px solid var(--line);padding:6px 0' }, h('div', { class: 'grow' }, h('div', {}, c.name), h('small', { class: 'muted' }, c.detail)), chipFor(c.status))));
}
function clipCard(c, { onChange, projects } = {}) {
  const attach = projects ? h('select', { onchange: (e) => act(null, async () => { await api(`/api/clips/${c.id}/attach`, { json: { project_id: e.target.value } }); toast('Attached. Re-run QA to check against the project.', 'ok'); onChange && onChange(); }) },
    h('option', { value: '' }, '— no project —'), projects.map(p => h('option', { value: p.id, selected: p.id === c.project_id }, `#${p.id} ${p.client}`))) : null;
  return h('div', { class: 'card clipv' },
    h('div', { class: 'row between' }, h('b', { class: 'grow' }, c.title || c.filename), chipFor(c.qa_status)),
    h('div', { class: 'muted small' }, `${c.aspect || ''} · ${c.duration ? fmtT(c.duration) : ''} · ${c.size ? (c.size / 1048576).toFixed(1) + ' MB' : ''}${c.captioned ? ' · captions' : ''}`),
    h('video', { controls: true, preload: 'metadata', playsinline: true, src: c.preview_url }),
    h('div', { class: 'row' },
      h('a', { class: 'btn small', href: c.url, download: c.filename }, 'Download'),
      h('button', { class: 'btn small', onclick: () => openSheet('Quality check', qaView(c.qa)) }, 'QA report'),
      h('button', { class: 'btn small', onclick: (e) => act(e.target, async () => { const r = await api(`/api/clips/${c.id}/qa`, { method: 'POST' }); watchJob(r.job_id, () => onChange && onChange()); }) }, 'Re-run QA'),
      h('button', { class: 'btn small danger', onclick: (e) => { if (confirm('Delete this clip file?')) act(e.target, async () => { await api(`/api/clips/${c.id}`, { method: 'DELETE' }); onChange && onChange(); }); } }, 'Delete')),
    attach ? field('Project', attach) : null);
}

// ------------------------------------------------------------------ HOME
async function viewHome(root) {
  const [d, st] = await Promise.all([api('/api/dashboard'), api('/api/status')]);
  state.status = st;
  const tile = (n, label, go) => h('button', { class: 'tile', onclick: () => location.hash = go }, h('b', {}, n), h('span', {}, label));
  const caps = [
    ['FFmpeg (video engine)', st.ffmpeg, 'Install in Termux: pkg install ffmpeg'],
    ['App password', st.password_set, 'Set APP_PASSWORD in .env'],
    ['Email sending (SMTP)', st.smtp_configured, 'Optional. Set SMTP_* in .env; otherwise messages are sent manually'],
    ['Stripe payment webhook', st.stripe_webhook_configured, 'Optional. Needs a Stripe account and a public HTTPS URL'],
    ['Auto feed scan', st.scan_interval_minutes > 0, 'Optional. Set SCAN_INTERVAL_MINUTES in .env']];
  mount(root, 
    h('div', { class: 'grid' }, tile(d.counts.leads_new, 'New leads', '#leads'), tile(d.counts.projects_open, 'Open projects', '#clients'), tile(d.counts.approvals_pending, 'Waiting for approval', '#approvals'), tile(d.counts.clips, 'Clips rendered', '#studio')),
    h('div', { class: 'panel' }, h('h2', {}, 'Earnings'), d.money.currencies.length ? d.money.currencies.map(c => h('div', { class: 'row between' }, h('b', {}, c.currency), h('span', {}, `paid ${c.paid.toFixed(2)} · pending ${c.pending.toFixed(2)} · net ${c.net.toFixed(2)}`))) : h('p', { class: 'muted' }, 'No payments recorded yet.'), h('p', { class: 'muted small' }, d.money.note)),
    h('div', { class: 'panel' }, h('h2', {}, 'Deadlines'), d.deadlines.length ? d.deadlines.map(p => h('div', { class: 'card click', onclick: () => location.hash = '#clients/' + p.id }, h('div', { class: 'row between' }, h('b', {}, p.client), h('span', { class: 'chip ' + (p.overdue ? 'bad' : '') }, p.deadline ? (p.overdue ? 'OVERDUE ' : '') + p.deadline : 'no deadline')), h('small', { class: 'muted' }, p.status))) : h('p', { class: 'muted' }, 'No open projects.')),
    h('div', { class: 'panel' }, h('h2', {}, 'System'), caps.map(([n, ok, hint]) => h('div', { class: 'row between', style: 'padding:4px 0' }, h('div', { class: 'grow' }, h('div', {}, n), ok ? null : h('small', { class: 'muted' }, hint)), h('span', { class: 'chip ' + (ok ? 'ok' : 'warn') }, ok ? 'on' : 'off'))),
      h('p', { class: 'muted small' }, 'Nothing is sent, accepted, delivered or spent without your approval in the Approve tab.'),
      st.password_set ? h('form', { method: 'post', action: '/logout' }, h('button', { class: 'btn ghost small' }, 'Sign out')) : null));
}

// ------------------------------------------------------------------ LEADS
async function viewLeads(root) {
  const q = state.filter === 'all' ? '' : state.filter === 'good' ? '?min_score=30' : '?status=' + state.filter;
  const leads = await api('/api/leads' + q);
  const f = (k, label) => h('button', { class: 'btn small ' + (state.filter === k ? 'on' : 'ghost'), onclick: () => { state.filter = k; route(); } }, label);
  mount(root, 
    h('div', { class: 'row' },
      h('button', { class: 'btn primary small', onclick: (e) => act(e.target, async () => { const r = await api('/api/scan', { method: 'POST' }); toast(`Scanned ${r.feeds_scanned} feed(s): ${r.new_leads} new`, r.feeds.some(x => x.error) ? 'bad' : 'ok'); route(); }) }, 'Scan feeds'),
      h('button', { class: 'btn small', onclick: feedsSheet }, 'Feeds'), h('button', { class: 'btn small', onclick: addLeadSheet }, 'Add lead'), h('button', { class: 'btn small', onclick: analyzeSheet }, 'Analyse brief')),
    h('div', { class: 'filters', style: 'margin-top:10px' }, f('all', 'All'), f('good', 'Good match'), f('new', 'New'), f('interested', 'Interested'), f('converted', 'Converted'), f('dismissed', 'Dismissed')),
    leads.length ? leads.map(leadCard) : h('div', { class: 'panel muted' }, 'No leads yet. Add an RSS/Atom feed under “Feeds”, or paste a listing with “Add lead”. Leads are suggestions only: nothing is contacted automatically.'));
}
function leadCard(l) {
  const col = l.score >= 50 ? 'ok' : l.score >= 25 ? 'warn' : '';
  return h('div', { class: 'card' },
    h('div', { class: 'row' }, h('div', { class: 'score chip ' + col }, l.score), h('div', { class: 'grow' }, h('b', {}, l.title), h('div', { class: 'muted small' }, `${l.status} · ${(l.created || '').slice(0, 10)}${l.matched ? ' · ' + l.matched : ''}`))),
    l.flags.length ? h('div', { class: 'row', style: 'margin-top:6px' }, l.flags.map(x => h('span', { class: 'chip bad' }, '⚠ ' + x.replace('_', ' ')))) : null,
    l.summary ? h('p', { class: 'muted small' }, l.summary.slice(0, 220) + (l.summary.length > 220 ? '…' : '')) : null,
    h('div', { class: 'row' },
      l.url && /^https?:/.test(l.url) ? h('a', { class: 'btn small', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open ↗') : null,
      h('button', { class: 'btn small', onclick: (e) => act(e.target, async () => openSheet('Brief analysis', ...analysisView(await api(`/api/leads/${l.id}/analyze`, { method: 'POST' })))) }, 'Analyse'),
      h('button', { class: 'btn small', onclick: () => draftSheet(l) }, 'Draft reply'),
      l.status !== 'converted' ? h('button', { class: 'btn small', onclick: (e) => act(e.target, async () => { const r = await api(`/api/leads/${l.id}/convert`, { method: 'POST' }); toast(r.note, 'ok'); location.hash = '#clients/' + r.project_id; }) }, 'Make project') : null,
      l.status === 'new' ? h('button', { class: 'btn small ghost', onclick: () => setLead(l.id, 'interested') }, 'Interested') : null,
      l.status !== 'dismissed' && l.status !== 'converted' ? h('button', { class: 'btn small ghost', onclick: () => setLead(l.id, 'dismissed') }, 'Dismiss') : null));
}
const setLead = (id, status) => act(null, async () => { await api(`/api/leads/${id}/status`, { json: { status } }); route(); });
function draftSheet(l) {
  const ch = h('select', {}, h('option', { value: 'manual' }, 'Manual: I send it myself'), h('option', { value: 'email', disabled: !state.status.smtp_configured }, state.status.smtp_configured ? 'Email via my SMTP' : 'Email (SMTP not configured)'));
  const to = h('input', { type: 'email', placeholder: 'client@example.com (email only)' });
  openSheet('Draft reply', h('p', { class: 'muted' }, 'This creates a draft in the Approve tab. Nothing is sent until you approve it there.'), field('Send method', ch), field('Recipient', to),
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { const r = await api(`/api/leads/${l.id}/draft`, { json: { channel: ch.value, to: to.value } }); closeSheet(); toast(r.note, 'ok'); location.hash = '#approvals'; }) }, 'Create draft for approval'));
}
function addLeadSheet() {
  const t = h('input', { placeholder: 'Job title' }), u = h('input', { type: 'url', placeholder: 'https://… (optional)' }), s = h('textarea', { rows: 6, placeholder: 'Paste the listing / brief text' });
  openSheet('Add lead', field('Title', t), field('Link', u), field('Brief text', s),
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { const r = await api('/api/leads', { json: { title: t.value, url: u.value, summary: s.value } }); closeSheet(); toast('Lead added (score ' + r.score + ')', 'ok'); route(); }) }, 'Save lead'));
}
function analyzeSheet() {
  const s = h('textarea', { rows: 8, placeholder: 'Paste a client message or job post' }), out = h('div');
  openSheet('Analyse a brief', field('Brief text', s), h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => out.replaceChildren(...analysisView(await api('/api/brief/analyze', { json: { text: s.value } })))) }, 'Analyse'), out);
}
async function feedsSheet() {
  const feeds = await api('/api/feeds'), u = h('input', { type: 'url', placeholder: 'https://example.com/jobs.rss' });
  openSheet('Opportunity feeds', h('p', { class: 'muted small' }, 'Add public RSS/Atom feeds from sites whose terms allow it. Login-only pages cannot be scanned. A site without a feed: paste listings with “Add lead”.'),
    feeds.map(f => h('div', { class: 'card' }, h('div', { class: 'row' }, h('div', { class: 'grow small', style: 'word-break:break-all' }, f.url, f.last_error ? h('div', { class: 'chip bad' }, f.last_error) : h('div', { class: 'muted' }, f.last_checked ? 'checked ' + f.last_checked.slice(0, 16) : 'not scanned yet')), h('button', { class: 'btn small danger', onclick: () => act(null, async () => { await api('/api/feeds/' + f.id, { method: 'DELETE' }); feedsSheet(); }) }, 'Remove')))),
    field('Add feed URL', u), h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { const fd = new FormData(); fd.append('url', u.value); const r = await api('/api/feeds', { form: fd }); toast(r.status, 'ok'); feedsSheet(); }) }, 'Add feed'));
}

// ------------------------------------------------------------------ STUDIO
async function viewStudio(root) {
  const [sources, clips, projects] = await Promise.all([api('/api/sources'), api('/api/clips'), api('/api/projects')]);
  const sel = sources.find(s => s.id === state.source) || null;
  if (!sel) { state.source = null; state.ranges = []; }
  const fileIn = h('input', { type: 'file', accept: 'video/*,.mkv', hidden: true, onchange: (e) => uploadSource(e.target.files[0], e.target) });
  mount(root, 
    h('div', { class: 'panel' }, h('h2', {}, '1 · Import a video'),
      h('div', { class: 'row' }, fileIn, h('button', { class: 'btn primary', onclick: () => fileIn.click() }, 'Upload from phone'), h('button', { class: 'btn', onclick: importPathSheet }, 'Phone folder path'), h('button', { class: 'btn', onclick: importUrlSheet }, 'Direct link')),
      h('p', { class: 'muted small' }, 'Only edit footage you own or have the client’s permission to use. “Direct link” needs a direct video file URL (e.g. a client’s download link), not a YouTube page.'),
      sources.map(s => h('div', { class: 'card click', style: s.id === state.source ? 'border-color:var(--accent)' : '', onclick: () => { state.source = s.id; state.ranges = []; route(); } },
        h('div', { class: 'row between' }, h('b', {}, s.filename.replace(/^[0-9a-f]{32}_/, '')), s.id === state.source ? h('span', { class: 'chip ok' }, 'selected') : null),
        h('small', { class: 'muted' }, `${fmtT(s.duration)} · ${s.width}×${s.height} · ${s.has_audio ? 'audio' : 'NO audio'}${s.has_captions ? ' · captions' : ''}${s.analysis ? ' · analysed' : ''}`)))),
    sel ? sourcePanel(sel, projects) : h('p', { class: 'muted pad' }, 'Select or import a video to find highlights and render clips.'),
    h('div', { class: 'panel' }, h('h2', {}, '4 · Rendered clips'), clips.length ? clips.map(c => clipCard(c, { onChange: route, projects })) : h('p', { class: 'muted' }, 'No clips yet.')));
}
async function uploadSource(file, input) {
  if (!file) return;
  toast('Uploading…'); const fd = new FormData(); fd.append('video', file);
  try { const s = await api('/api/sources/upload', { form: fd }); state.source = s.id; state.ranges = []; toast('Imported', 'ok'); route(); } catch (e) { fail(e); } finally { input.value = ''; }
}
function importPathSheet() {
  const p = h('input', { placeholder: '/storage/emulated/0/Movies/podcast.mp4' });
  const dirs = (state.status && state.status.import_dirs) || [];
  openSheet('Import from phone storage', h('p', { class: 'muted small' }, 'The file is used in place (no copy). In Termux run termux-setup-storage once, then use ~/storage/movies/… Allowed folders: ' + (dirs.join(', ') || 'none yet')),
    field('File path', p), h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { const s = await api('/api/sources/import-path', { json: { path: p.value } }); state.source = s.id; closeSheet(); route(); }) }, 'Import'));
}
function importUrlSheet() {
  const u = h('input', { type: 'url', placeholder: 'https://…/video.mp4' });
  openSheet('Import from direct link', field('Video file URL', u), h('p', { class: 'muted small' }, 'Private/local addresses are blocked.'),
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { const r = await api('/api/sources/import-url', { json: { url: u.value } }); closeSheet(); toast('Downloading…'); watchJob(r.job_id, (j) => { if (j.status === 'done') { state.source = j.result.id; route(); } }); }) }, 'Download'));
}
function sourcePanel(s, projects) {
  const player = h('video', { controls: true, preload: 'metadata', playsinline: true, src: `/api/sources/${s.id}/file` });
  const capIn = h('input', { type: 'file', accept: '.srt,.vtt,text/vtt,application/x-subrip', hidden: true, onchange: (e) => act(null, async () => { const fd = new FormData(); fd.append('captions', e.target.files[0]); const r = await api(`/api/sources/${s.id}/captions`, { form: fd }); toast(`${r.cues} caption lines loaded`, 'ok'); route(); }) });
  const len = h('input', { type: 'number', value: 30, min: 5, max: 180 }), cnt = h('input', { type: 'number', value: 5, min: 1, max: 20 });
  const sc = h('select', {}, h('option', { value: 'auto' }, 'Auto (skip for videos over 30 min)'), h('option', { value: 'true' }, 'Yes (slower)'), h('option', { value: 'false' }, 'No (faster)'));
  const toggle = (hl) => { const i = state.ranges.findIndex(r => r.start === hl.start && r.end === hl.end); if (i >= 0) state.ranges.splice(i, 1); else state.ranges.push({ start: hl.start, end: hl.end, title: hl.title || '' }); route(); };
  const a = s.analysis;
  const manualS = h('input', { type: 'number', min: 0, step: 0.5, placeholder: 'start (s)' }), manualE = h('input', { type: 'number', min: 1, step: 0.5, placeholder: 'end (s)' });
  const preset = h('select', {}, h('option', { value: '' }, 'Custom'), Object.entries(state.status.presets).map(([k, v]) => h('option', { value: k, selected: k === 'tiktok' }, `${v.label} (${v.aspect}, ≤${v.max_seconds}s advisory)`)));
  const aspect = h('select', {}, state.status.aspects.map(x => h('option', { value: x }, x)));
  preset.onchange = () => { const p = state.status.presets[preset.value]; if (p) aspect.value = p.aspect; }; preset.onchange();
  const fit = h('select', {}, h('option', { value: 'crop' }, 'Fill frame (centre crop)'), h('option', { value: 'blur' }, 'Fit whole picture on blurred background'));
  const [capL, capI] = checkbox('Burn in captions (needs SRT/VTT)', s.has_captions); capI.disabled = !s.has_captions;
  const [normL, normI] = checkbox('Normalise loudness', true);
  const proj = h('select', {}, h('option', { value: '' }, '— no project —'), projects.filter(p => !['closed', 'delivered'].includes(p.status)).map(p => h('option', { value: p.id }, `#${p.id} ${p.client}`)));
  return h('div', {},
    h('div', { class: 'panel' }, h('h2', {}, '2 · Find highlights'), player,
      h('div', { class: 'row', style: 'margin-top:8px' }, capIn, h('button', { class: 'btn small', onclick: () => capIn.click() }, s.has_captions ? 'Replace captions (SRT/VTT)' : 'Add captions (SRT/VTT)'),
        h('button', { class: 'btn small danger', onclick: () => { if (confirm('Remove this source video? Rendered clips are kept.')) act(null, async () => { await api('/api/sources/' + s.id, { method: 'DELETE' }); state.source = null; route(); }); } }, 'Remove source')),
      h('p', { class: 'muted small' }, 'Highlights are ranked from audio energy, pace and (if analysed) scene changes, plus your captions if provided. This is a signal heuristic, not understanding of meaning: always preview before rendering.'),
      s.has_audio ? null : h('div', { class: 'banner warn' }, 'No audio track: use manual ranges below.'),
      h('div', { class: 'grid' }, field('Clip length (s)', len), field('How many', cnt)), field('Scene-change analysis', sc),
      h('button', { class: 'btn primary block', disabled: !s.has_audio, onclick: (e) => act(e.target, async () => { const r = await api(`/api/sources/${s.id}/analyze`, { json: { clip_seconds: +len.value, count: +cnt.value, use_scenes: sc.value } }); state.ranges = []; watchJob(r.job_id, route); }) }, 'Find highlights'),
      a ? h('div', {}, h('h3', {}, `Suggestions (${a.highlights.length})`), a.highlights.length ? a.highlights.map(hl => {
        const on = state.ranges.some(r => r.start === hl.start && r.end === hl.end);
        return h('div', { class: 'card' }, h('div', { class: 'row' }, h('div', { class: 'score chip' }, Math.round(hl.score)), h('div', { class: 'grow' }, h('b', {}, `${fmtT(hl.start)} – ${fmtT(hl.end)}`, h('span', { class: 'muted' }, `  (${Math.round(hl.duration)}s)`)), h('div', { class: 'muted small' }, hl.reason + (hl.title ? ' · “' + hl.title + '”' : '')))),
          h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: () => { player.currentTime = hl.start; player.play(); player.scrollIntoView({ block: 'center' }); } }, '▶ Preview'), h('button', { class: 'btn small ' + (on ? 'primary' : ''), onclick: () => toggle(hl) }, on ? '✓ Selected' : 'Select')));
      }) : h('p', { class: 'muted' }, 'Nothing found. Try a shorter clip length, or add ranges manually.')) : null),
    h('div', { class: 'panel' }, h('h2', {}, '3 · Render'),
      h('div', { class: 'grid' }, field('Start (s)', manualS), field('End (s)', manualE)),
      h('button', { class: 'btn small', onclick: () => { const st = +manualS.value, en = +manualE.value; if (!(en > st)) return fail(new Error('End must be after start')); state.ranges.push({ start: st, end: en, title: '' }); route(); } }, '+ Add manual range'),
      state.ranges.length ? state.ranges.map((r, i) => h('div', { class: 'row between card' }, h('span', {}, `${fmtT(r.start)} – ${fmtT(r.end)} (${Math.round(r.end - r.start)}s)`), h('button', { class: 'btn small ghost', onclick: () => { state.ranges.splice(i, 1); route(); } }, 'Remove'))) : h('p', { class: 'muted' }, 'Select highlights above or add manual ranges.'),
      field('Platform preset', preset), field('Aspect ratio', aspect), field('Framing', fit), capL, normL, field('Check against project (optional)', proj),
      h('button', { class: 'btn primary block', disabled: !state.ranges.length, onclick: (e) => act(e.target, async () => {
        const r = await api('/api/render', { json: { source_id: s.id, ranges: state.ranges, preset: preset.value, aspect: aspect.value, fit: fit.value, captions: capI.checked, normalize: normI.checked, project_id: proj.value } });
        state.ranges = []; toast('Rendering started'); watchJob(r.job_id, route);
      }) }, `Render ${state.ranges.length || ''} clip${state.ranges.length === 1 ? '' : 's'}`)));
}

// ------------------------------------------------------------------ CLIENTS
async function viewClients(root, id) { return id ? viewProject(root, +id) : viewProjectList(root); }
async function viewProjectList(root) {
  const ps = await api('/api/projects');
  mount(root, h('button', { class: 'btn primary', onclick: newProjectSheet }, '+ New project'),
    ps.length ? ps.map(p => h('div', { class: 'card click', onclick: () => location.hash = '#clients/' + p.id },
      h('div', { class: 'row between' }, h('b', {}, p.client), statusChip(p.status)), 
      h('div', { class: 'row small muted' }, h('span', {}, p.status), p.deadline ? h('span', { class: p.overdue ? 'chip bad' : '' }, (p.overdue ? 'OVERDUE ' : 'due ') + p.deadline) : null, h('span', {}, fmtMoney(p.price, p.currency)), h('span', {}, `rev ${p.revisions_used}/${p.revisions_allowed}`))))
      : h('div', { class: 'panel muted' }, 'No projects. Create one, or press “Make project” on a lead.'));
}
function newProjectSheet() {
  const c = h('input', { placeholder: 'Client / business' }), e = h('input', { type: 'email', placeholder: 'optional' }), pr = h('input', { type: 'number', min: 0, step: 0.01, value: 0 });
  const cur = h('select', {}, ['USD', 'PKR', 'EUR', 'GBP'].map(x => h('option', {}, x))), dl = h('input', { type: 'date' }), rv = h('input', { type: 'number', min: 0, value: 2 }), b = h('textarea', { rows: 5, placeholder: 'Paste the client’s requirements: platforms, length, captions, deadline…' });
  openSheet('New project', field('Client', c), field('Email', e), h('div', { class: 'grid' }, field('Price', pr), field('Currency', cur)), h('div', { class: 'grid' }, field('Deadline', dl), field('Revisions included', rv)), field('Brief', b),
    h('p', { class: 'muted small' }, 'Requirements (format, length, captions) are read from the brief automatically; you can correct them on the project page.'),
    h('button', { class: 'btn primary block', onclick: (ev) => act(ev.target, async () => { const r = await api('/api/projects', { json: { client: c.value, email: e.value, price: pr.value, currency: cur.value, deadline: dl.value, revisions_allowed: rv.value, brief: b.value } }); closeSheet(); location.hash = '#clients/' + r.id; }) }, 'Create'));
}
async function viewProject(root, id) {
  const d = await api('/api/projects/' + id), p = d.project, projects = [p];
  const allowed = ['lead', 'quoted', 'review', 'closed'].concat(p.accepted_at ? ['in_progress'] : []);
  const st = h('select', {}, allowed.concat(allowed.includes(p.status) ? [] : [p.status]).map(x => h('option', { value: x, selected: x === p.status }, x)));
  const req = p.requirements || {};
  mount(root, 
    h('a', { href: '#clients', class: 'muted' }, '‹ All projects'),
    h('div', { class: 'panel' }, h('div', { class: 'row between' }, h('h2', { style: 'margin:0' }, p.client), statusChip(p.status)),
      h('div', { class: 'kv' }, h('span', {}, 'Status'), h('span', {}, p.status + (p.accepted_at ? ' (contract accepted)' : '')), h('span', {}, 'Deadline'), h('span', { class: p.overdue ? 'chip bad' : '' }, p.deadline || 'not set'), h('span', {}, 'Price'), h('span', {}, fmtMoney(p.price, p.currency)), h('span', {}, 'Paid / balance'), h('span', {}, `${p.paid.toFixed(2)} / ${p.balance.toFixed(2)}`), h('span', {}, 'Revisions'), h('span', { class: p.revisions_used > p.revisions_allowed ? 'chip warn' : '' }, `${p.revisions_used} of ${p.revisions_allowed} used`), p.email ? [h('span', {}, 'Email'), h('span', {}, p.email)] : null),
      h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn small', onclick: () => editProjectSheet(p) }, 'Edit'), h('a', { class: 'btn small', href: `/projects/${p.id}/invoice`, target: '_blank', rel: 'noopener' }, 'Invoice'), h('span', { class: 'grow' }), st,
        h('button', { class: 'btn small', onclick: () => act(null, async () => { await api(`/api/projects/${p.id}/status`, { json: { status: st.value } }); toast('Status updated', 'ok'); route(); }) }, 'Set status'))),
    h('div', { class: 'panel' }, h('h2', {}, 'Brief & requirements'), p.brief ? h('pre', { class: 'msg' }, p.brief) : h('p', { class: 'muted' }, 'No brief saved. Add one with Edit.'),
      h('div', { class: 'kv' }, h('span', {}, 'Format'), h('span', {}, req.aspect || '—'), h('span', {}, 'Length'), h('span', {}, req.max_seconds ? `${req.min_seconds ? req.min_seconds + '–' : '≤'}${req.max_seconds}s` : '—'), h('span', {}, 'Captions'), h('span', {}, req.captions ? 'required' : 'no'), h('span', {}, 'Quantity'), h('span', {}, req.count || '—'), h('span', {}, 'Max size'), h('span', {}, req.max_mb ? req.max_mb + ' MB' : '—')),
      h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: () => requirementsSheet(p) }, 'Edit requirements'),
        h('button', { class: 'btn small', onclick: (e) => act(e.target, async () => { const a = await api(`/api/projects/${p.id}/reanalyze`, { method: 'POST' }); openSheet('Brief analysis', ...analysisView(a)); route(); }) }, 'Re-read brief'),
        p.analysis ? h('button', { class: 'btn small', onclick: () => openSheet('Brief analysis', ...analysisView(p.analysis)) }, 'View analysis') : null)),
    h('div', { class: 'panel' }, h('h2', {}, 'Approvals needed'),
      h('p', { class: 'muted small' }, 'These only create a request. You approve or reject it in the Approve tab.'),
      h('div', { class: 'row' }, !p.accepted_at ? h('button', { class: 'btn small', onclick: (e) => act(e.target, async () => { await api(`/api/projects/${p.id}/request-accept`, { method: 'POST' }); toast('Request created. Open the Approve tab.', 'ok'); route(); }) }, 'Accept contract…') : null,
        h('button', { class: 'btn small', onclick: () => commitmentSheet(p) }, 'Quote / deadline…'), h('button', { class: 'btn small', disabled: !d.clips.length, onclick: () => deliverySheet(p, d.clips) }, 'Deliver clips…'))),
    h('div', { class: 'panel' }, h('h2', {}, `Clips (${d.clips.length})`), d.clips.length ? d.clips.map(c => clipCard(c, { onChange: route })) : h('p', { class: 'muted' }, 'No clips attached. In Studio, pick this project when rendering, or attach existing clips there.')),
    revisionsPanel(p, d.revisions),
    h('div', { class: 'panel' }, h('h2', {}, 'Payments'), d.payments.length ? d.payments.map(x => h('div', { class: 'row between' }, h('span', {}, `${fmtMoney(x.amount, x.currency)} ${x.reference || ''}`), chipFor(x.status))) : h('p', { class: 'muted' }, 'None recorded. Use the Money tab.'), d.deliveries.length ? [h('h3', {}, 'Deliveries'), d.deliveries.map(x => h('div', { class: 'small muted' }, `${x.created.slice(0, 16)} · ${x.method}`))] : null),
    d.approvals.length ? h('div', { class: 'panel' }, h('h2', {}, 'Approval history'), d.approvals.map(a => h('div', { class: 'row between small' }, h('span', {}, a.title), chipFor(a.status)))) : null);
}
function revisionsPanel(p, revs) {
  const n = h('textarea', { rows: 2, placeholder: 'What did the client ask to change?' });
  return h('div', { class: 'panel' }, h('h2', {}, 'Revision requests'), n,
    h('button', { class: 'btn small', onclick: (e) => act(e.target, async () => { const r = await api(`/api/projects/${p.id}/revisions`, { json: { note: n.value } }); toast(r.over_limit ? 'Over the agreed revisions: ' + r.note : 'Revision logged', r.over_limit ? 'bad' : 'ok'); route(); }) }, 'Log revision'),
    revs.map(r => h('div', { class: 'card' }, h('div', { class: 'row between' }, h('span', {}, r.note), r.status === 'open' ? h('button', { class: 'btn small', onclick: () => act(null, async () => { await api(`/api/revisions/${r.id}/done`, { method: 'POST' }); route(); }) }, 'Done') : chipFor('pass')))));
}
function editProjectSheet(p) {
  const f = { client: h('input', { value: p.client }), email: h('input', { type: 'email', value: p.email || '' }), price: h('input', { type: 'number', min: 0, step: 0.01, value: p.price }), currency: h('input', { value: p.currency }), deadline: h('input', { type: 'date', value: p.deadline || '' }),
    revisions_allowed: h('input', { type: 'number', min: 0, value: p.revisions_allowed }), payment_link: h('input', { type: 'url', value: p.payment_link || '', placeholder: 'Your own Stripe/PayPal payment link (optional)' }), brief: h('textarea', { rows: 5, value: p.brief || '' }), notes: h('textarea', { rows: 3, value: p.notes || '' }) };
  openSheet('Edit project', Object.entries(f).map(([k, el]) => field(k.replace('_', ' '), el)), h('p', { class: 'muted small' }, 'Edits here are your own bookkeeping. To commit to a price/deadline with the client, use “Quote / deadline…” which needs approval.'),
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { await api(`/api/projects/${p.id}/update`, { json: Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value])) }); closeSheet(); route(); }) }, 'Save'));
}
function requirementsSheet(p) {
  const r = p.requirements || {}, asp = h('select', {}, h('option', { value: '' }, 'any'), ['9:16', '1:1', '16:9', '4:5'].map(x => h('option', { value: x, selected: r.aspect === x }, x)));
  const mn = h('input', { type: 'number', min: 0, value: r.min_seconds ?? '' }), mx = h('input', { type: 'number', min: 0, value: r.max_seconds ?? '' }), cnt = h('input', { type: 'number', min: 0, value: r.count ?? '' }), mb = h('input', { type: 'number', min: 0, value: r.max_mb ?? '' }), [capL, capI] = checkbox('Captions required', !!r.captions);
  openSheet('Requirements to check before delivery', field('Aspect ratio', asp), h('div', { class: 'grid' }, field('Min length (s)', mn), field('Max length (s)', mx)), h('div', { class: 'grid' }, field('Quantity', cnt), field('Max file size (MB)', mb)), capL,
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { const x = await api(`/api/projects/${p.id}/requirements`, { json: { aspect: asp.value, min_seconds: mn.value, max_seconds: mx.value, count: cnt.value, max_mb: mb.value, captions: capI.checked } }); closeSheet(); toast(x.note, 'ok'); route(); }) }, 'Save'));
}
function commitmentSheet(p) {
  const pr = h('input', { type: 'number', min: 0, step: 0.01, placeholder: String(p.price || '') }), dl = h('input', { type: 'date', value: p.deadline || '' }), tx = h('textarea', { rows: 3, placeholder: 'What exactly are you committing to?' });
  openSheet('Request approval: quote / deadline', h('p', { class: 'muted small' }, 'Approving records this on the project. You still tell the client yourself (or via an approved message).'), field(`Price (${p.currency})`, pr), field('Deadline', dl), field('Commitment note', tx),
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { await api(`/api/projects/${p.id}/request-commitment`, { json: { price: pr.value, deadline: dl.value, text: tx.value } }); closeSheet(); toast('Request created: see Approve tab', 'ok'); route(); }) }, 'Create request'));
}
function deliverySheet(p, clips) {
  const boxes = clips.map(c => { const i = h('input', { type: 'checkbox', checked: c.qa_status === 'pass' || c.qa_status === 'warn' }); return [h('label', { class: 'check' }, i, `${c.title || c.filename} `, chipFor(c.qa_status)), i, c]; });
  const msg = h('textarea', { rows: 4, value: `Hi, your clips are ready. Please let me know if you would like any changes.` }), to = h('input', { type: 'email', value: p.email || '' });
  const ch = h('select', {}, h('option', { value: 'manual' }, 'Manual: I send the files myself'), h('option', { value: 'email', disabled: !state.status.smtp_configured }, state.status.smtp_configured ? 'Email with attachments (≤20 MB)' : 'Email (SMTP not configured)'));
  const [ovL, ovI] = checkbox('Deliver even if QA did not pass (I accept the risk)', false);
  openSheet('Request approval: deliver clips', h('p', { class: 'muted small' }, 'Clips that failed QA are blocked unless you tick the override.'), boxes.map(b => b[0]), field('Message', msg), field('Send method', ch), field('Recipient (email only)', to), ovL,
    h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { await api(`/api/projects/${p.id}/request-delivery`, { json: { clip_ids: boxes.filter(b => b[1].checked).map(b => b[2].id), message: msg.value, channel: ch.value, to: to.value, override_qa: ovI.checked } }); closeSheet(); toast('Request created: see Approve tab', 'ok'); route(); }) }, 'Create request'));
}

// ------------------------------------------------------------------ APPROVALS
const KIND = { send_message: 'Send message', accept_contract: 'Accept contract', commitment: 'Commit to price/deadline', deliver: 'Deliver to client', spend_money: 'Record spending' };
async function viewApprovals(root) {
  const rows = await api('/api/approvals' + (state.apprTab === 'open' ? '?status=open' : ''));
  const tab = (k, l) => h('button', { class: 'btn small ' + (state.apprTab === k ? 'on' : 'ghost'), onclick: () => { state.apprTab = k; route(); } }, l);
  mount(root, h('div', { class: 'filters' }, tab('open', 'Waiting for you'), tab('all', 'History')), h('p', { class: 'muted small' }, 'Nothing here happens until you tap Approve. Rejected items do nothing.'),
    rows.length ? rows.map(approvalCard) : h('div', { class: 'panel muted' }, state.apprTab === 'open' ? 'Nothing is waiting for approval.' : 'No history yet.'));
}
function approvalCard(a) {
  const p = a.payload, open = a.status === 'pending' || a.status === 'failed', inputs = {};
  const body = [];
  if (a.kind === 'send_message') {
    inputs.subject = h('input', { value: p.subject || '', disabled: !open }); inputs.to = h('input', { value: p.to || '', disabled: !open, placeholder: p.channel === 'email' ? 'recipient email' : '(manual: not used)' }); inputs.body = h('textarea', { rows: 9, value: p.body || '', disabled: !open });
    body.push(h('div', { class: 'row small' }, h('span', { class: 'chip' }, 'via ' + p.channel)), field('To', inputs.to), field('Subject', inputs.subject), field('Message', inputs.body));
  } else if (a.kind === 'deliver') {
    inputs.message = h('textarea', { rows: 4, value: p.message || '', disabled: !open });
    body.push(h('div', { class: 'small' }, `${p.clip_ids.length} clip(s) via ${p.channel}${p.to ? ' to ' + p.to : ''}`), p.override_qa ? h('div', { class: 'banner warn' }, 'QA override is ON: some clips may not meet requirements.') : null, field('Message', inputs.message));
  } else if (a.kind === 'commitment') body.push(h('div', { class: 'kv' }, h('span', {}, 'Price'), h('span', {}, p.price != null ? fmtMoney(p.price, p.currency || '') : '—'), h('span', {}, 'Deadline'), h('span', {}, p.deadline || '—'), h('span', {}, 'Note'), h('span', {}, p.text || '—')));
  else if (a.kind === 'spend_money') body.push(h('div', { class: 'kv' }, h('span', {}, 'Amount'), h('span', {}, fmtMoney(p.amount, p.currency)), h('span', {}, 'For'), h('span', {}, p.description || '—')), h('p', { class: 'muted small' }, 'The app never moves money. This only records an expense you approve and pay yourself.'));
  else if (a.kind === 'accept_contract') body.push(h('p', {}, 'Marks the project as accepted and in progress. Accept on the marketplace yourself if the job is hosted there.'));
  const edits = () => Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
  return h('div', { class: 'card' }, h('div', { class: 'row between' }, h('b', { class: 'grow' }, a.title), chipFor(a.status)), h('div', { class: 'muted small' }, `${KIND[a.kind] || a.kind} · ${a.created.slice(0, 16)}`), body,
    a.result ? h('div', { class: 'banner ' + (a.status === 'failed' ? 'bad' : 'ok') }, a.result) : null,
    open ? h('div', { class: 'row' },
      h('button', { class: 'btn primary', onclick: (e) => { if (confirm(`Approve: ${a.title}?`)) act(e.target, async () => { if (Object.keys(inputs).length && a.status === 'pending') await api(`/api/approvals/${a.id}/edit`, { json: edits() }); const r = await api(`/api/approvals/${a.id}/approve`, { method: 'POST' }); toast(r.result, r.status === 'failed' ? 'bad' : 'ok'); route(); }); } }, a.status === 'failed' ? 'Retry' : 'Approve'),
      h('button', { class: 'btn danger', onclick: (e) => act(e.target, async () => { await api(`/api/approvals/${a.id}/reject`, { method: 'POST' }); route(); }) }, 'Reject')) : null,
    a.status === 'approved' && a.kind === 'send_message' ? h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: () => copyText(`${p.subject ? p.subject + '\n\n' : ''}${p.body}`).then(() => toast('Copied', 'ok')) }, 'Copy message'), h('button', { class: 'btn small primary', onclick: () => act(null, async () => { await api(`/api/approvals/${a.id}/mark-sent`, { method: 'POST' }); route(); }) }, 'I sent it')) : null);
}

// ------------------------------------------------------------------ MONEY
async function viewMoney(root) {
  const [sum, pays, exps, projects] = await Promise.all([api('/api/money/summary'), api('/api/payments'), api('/api/expenses'), api('/api/projects')]);
  const proj = h('select', {}, h('option', { value: '' }, '— none —'), projects.map(p => h('option', { value: p.id }, `#${p.id} ${p.client}`)));
  const amt = h('input', { type: 'number', min: 0.01, step: 0.01 }), cur = h('select', {}, ['PKR', 'USD', 'EUR', 'GBP'].map(x => h('option', {}, x))), stt = h('select', {}, ['pending', 'paid', 'refunded'].map(x => h('option', {}, x))), ref = h('input', { placeholder: 'transaction reference (optional)' });
  mount(root, 
    h('div', { class: 'panel' }, h('h2', {}, 'Earnings'), sum.currencies.length ? sum.currencies.map(c => h('div', { class: 'card' }, h('b', {}, c.currency), h('div', { class: 'kv' }, h('span', {}, 'Paid'), h('span', {}, c.paid.toFixed(2)), h('span', {}, 'Pending'), h('span', {}, c.pending.toFixed(2)), h('span', {}, 'Expenses'), h('span', {}, c.expenses.toFixed(2)), h('span', {}, 'Net'), h('b', {}, c.net.toFixed(2))))) : h('p', { class: 'muted' }, 'Nothing recorded yet.'), h('p', { class: 'muted small' }, sum.note)),
    h('div', { class: 'panel' }, h('h2', {}, 'Record a payment'), h('p', { class: 'muted small' }, 'Mark “paid” only after you have seen the money arrive. This does not collect money.'), field('Project', proj), h('div', { class: 'grid' }, field('Amount', amt), field('Currency', cur)), field('Status', stt), field('Reference', ref),
      h('button', { class: 'btn primary block', onclick: (e) => act(e.target, async () => { await api('/api/payments', { json: { project_id: proj.value, amount: amt.value, currency: cur.value, status: stt.value, reference: ref.value } }); toast('Recorded', 'ok'); route(); }) }, 'Save record')),
    h('div', { class: 'panel' }, h('div', { class: 'row between' }, h('h2', { style: 'margin:0' }, 'Ledger'), h('a', { class: 'btn small', href: '/api/money/export.csv' }, 'Export CSV')),
      pays.length ? pays.map(x => h('div', { class: 'card' }, h('div', { class: 'row between' }, h('b', {}, fmtMoney(x.amount, x.currency)), h('select', { onchange: (e) => act(null, async () => { await api(`/api/payments/${x.id}/status`, { json: { status: e.target.value } }); route(); }) }, ['pending', 'paid', 'refunded'].map(s => h('option', { selected: s === x.status }, s)))), h('div', { class: 'muted small' }, `${x.client || 'no project'} · ${x.source} · ${x.created.slice(0, 10)} ${x.reference || ''}`))) : h('p', { class: 'muted' }, 'Empty.')),
    spendPanel(exps, projects),
    h('div', { class: 'panel' }, h('h2', {}, 'Automatic payment tracking'), h('p', { class: 'muted' }, state.status.stripe_webhook_configured ? 'Stripe webhook is configured: verified “paid” events from your own Stripe account are added to the ledger.' : 'Optional: if you have a Stripe account, set STRIPE_WEBHOOK_SECRET in .env and point a Stripe webhook at /webhooks/stripe on a public HTTPS URL. Without it, record payments by hand above.')));
}
function spendPanel(exps, projects) {
  const d = h('input', { placeholder: 'What for?' }), a = h('input', { type: 'number', min: 0.01, step: 0.01 }), c = h('select', {}, ['PKR', 'USD', 'EUR', 'GBP'].map(x => h('option', {}, x)));
  return h('div', { class: 'panel' }, h('h2', {}, 'Expenses'), h('p', { class: 'muted small' }, 'Any spending must be approved first. The app never pays anything itself.'), field('Description', d), h('div', { class: 'grid' }, field('Amount', a), field('Currency', c)),
    h('button', { class: 'btn block', onclick: (e) => act(e.target, async () => { await api('/api/spend-request', { json: { description: d.value, amount: a.value, currency: c.value } }); toast('Request created: see Approve tab', 'ok'); }) }, 'Request approval'),
    exps.map(x => h('div', { class: 'row between small' }, h('span', {}, `${x.description} · ${fmtMoney(x.amount, x.currency)}`), chipFor(x.status))));
}

// ------------------------------------------------------------------ router
const views = { home: viewHome, leads: viewLeads, studio: viewStudio, clients: viewClients, approvals: viewApprovals, money: viewMoney };
let routing = 0;
async function route() {
  const [name, arg] = (location.hash.slice(1) || 'home').split('/');
  const view = views[name] ? name : 'home', my = ++routing, root = $('#view');
  document.querySelectorAll('#tabs a').forEach(a => a.classList.toggle('on', a.dataset.tab === view));
  try { if (!state.status) state.status = await api('/api/status'); await views[view](root, arg); }
  catch (e) { if (my === routing) mount(root, h('div', { class: 'banner bad' }, 'Could not load: ' + e.message), h('button', { class: 'btn', onclick: route }, 'Try again')); }
  refreshBadge();
}
async function refreshBadge() { try { const n = (await api('/api/approvals?status=open')).length, b = $('#apprBadge'); b.textContent = n; b.hidden = !n; } catch (_) { /* ignore */ } }

$('#sheetClose').addEventListener('click', closeSheet);
$('#sheet').addEventListener('click', (e) => { if (e.target.id === 'sheet') closeSheet(); });
$('#refreshBtn').addEventListener('click', route);
window.addEventListener('hashchange', () => { closeSheet(); route(); });
setInterval(refreshBadge, 20000);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
route(); pollJobs();

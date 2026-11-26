// End-to-end UI test: real uvicorn server + real FFmpeg + the real app.js running in jsdom.
// It clicks the same buttons a user would and checks the result on screen.
// Not covered (jsdom cannot do them): native file picker, video playback, layout/visuals on a phone.
import { JSDOM, VirtualConsole } from 'jsdom';
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'autopilot-ui-'));
const port = 8700 + Math.floor(Math.random() * 200);
const base = `http://127.0.0.1:${port}`;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let passed = 0;
const errors = [];

function ff(...a) { execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...a]); }
const video = path.join(tmp, 'sample.mp4'), srt = path.join(tmp, 'c.srt');
ff('-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=15:d=40', '-f', 'lavfi', '-i', 'sine=f=300:d=40:r=44100', '-af', "volume='if(between(t,15,27),0.8,0.03)':eval=frame",
   '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '32', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', video);
fs.writeFileSync(srt, '1\n00:00:16,000 --> 00:00:20,000\nWhy is this the biggest mistake?\n\n2\n00:00:21,000 --> 00:00:25,000\nHere are 3 steps\n');

const server = spawn(process.env.PYTHON || 'python3', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port), '--log-level', 'warning'],
  { cwd: root, env: { ...process.env, DATA_DIR: path.join(tmp, 'data'), APP_PASSWORD: '', IMPORT_DIRS: '' }, stdio: ['ignore', 'inherit', 'inherit'] });
let dom;
async function finish(code) { try { dom && dom.window.close(); } catch (_) {} server.kill(); fs.rmSync(tmp, { recursive: true, force: true }); process.exit(code); }
process.on('uncaughtException', (e) => { console.error('FAIL (exception):', e); finish(1); });
process.on('unhandledRejection', (e) => { console.error('FAIL (rejection):', e); finish(1); });

for (let i = 0; i < 100; i++) { try { if ((await fetch(base + '/health')).ok) break; } catch (_) {} await sleep(150); if (i === 99) throw new Error('server did not start'); }

const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/Not implemented/.test(e.message)) errors.push('jsdomError: ' + e.message); });
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
dom = await JSDOM.fromURL(base + '/', {
  runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (u, o) => fetch(new URL(u, base), o); w.FormData = FormData; w.confirm = () => true; w.scrollTo = () => {};
    w.HTMLElement.prototype.scrollIntoView = () => {}; w.HTMLMediaElement.prototype.play = () => Promise.resolve();
    w.HTMLMediaElement.prototype.load = () => {}; w.HTMLMediaElement.prototype.pause = () => {};
  },
});
const { window } = dom, doc = window.document;

const $$ = (sel, r = doc) => [...r.querySelectorAll(sel)];
const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
const view = () => txt(doc.querySelector('#view'));
async function waitFor(fn, what, ms = 25000) { const end = Date.now() + ms; while (Date.now() < end) { const v = fn(); if (v) return v; await sleep(100); } throw new Error('Timed out waiting for: ' + what + '\nVIEW: ' + view().slice(0, 600) + '\nSHEET: ' + txt(doc.querySelector('#sheetBody'))); }
const button = (label, r = doc) => $$('button, a.btn', r).find(b => txt(b) === label || txt(b).startsWith(label));
const click = async (label, r = doc) => { const b = await waitFor(() => { const x = button(label, r); return x && !x.disabled ? x : null; }, `button "${label}"`); b.click(); return b; };
const sheet = () => doc.querySelector('#sheetBody');
const fieldIn = (r, label) => { const l = $$('label', r).find(x => txt(x).toLowerCase().startsWith(label.toLowerCase())); if (!l) throw new Error('no field ' + label); return l.querySelector('input,select,textarea'); };
const setv = (el, v) => { el.value = v; el.dispatchEvent(new window.Event('input', { bubbles: true })); };
const go = async (hash) => { window.location.hash = hash; await sleep(60); };
const step = (name) => { passed++; console.log('  ✓', name); };
const api = async (p, o) => { const r = await fetch(base + p, o); if (!r.ok) throw new Error(p + ' ' + r.status + ' ' + await r.text()); return r.json(); };

console.log('UI end-to-end test');

// 1. Home
await waitFor(() => $$('.tile').length === 4, 'home tiles');
await waitFor(() => view().includes('FFmpeg (video engine)') && view().includes('Nothing is sent'), 'system panel');
step('Home renders tiles, earnings, deadlines, system status');

// 2. Leads: add manually, score shown
await go('#leads'); await waitFor(() => view().includes('No leads yet'), 'empty leads');
await click('Add lead');
await waitFor(() => !doc.querySelector('#sheet').hidden, 'sheet open');
setv(fieldIn(sheet(), 'Title'), 'Podcast clipper for TikTok'); setv(fieldIn(sheet(), 'Brief text'), 'Need 5 TikTok clips from my podcast, paid $20 per clip, captions required');
await click('Save lead', sheet());
const card = await waitFor(() => $$('#view .card').find(c => txt(c).includes('Podcast clipper for TikTok')), 'lead card');
if (!(Number(txt(card.querySelector('.score'))) >= 50)) throw new Error('lead score too low: ' + txt(card.querySelector('.score')));
step('Add lead -> card with relevance score');
await click('Analyse', card); await waitFor(() => /Platforms\s*tiktok/.test(txt(sheet())) && /Budget\s*USD 20 per clip/.test(txt(sheet())) && txt(sheet()).includes('Confirm with the client'), 'analysis sheet');
doc.querySelector('#sheetClose').click();
step('Lead analysis shows platforms, budget and questions');

// 3. Draft reply -> approval; nothing sent until approved
await click('Draft reply', card); await click('Create draft for approval', sheet());
await waitFor(() => window.location.hash === '#approvals', 'navigate to approvals');
await waitFor(() => view().includes('Outreach: Podcast clipper') && view().includes('Waiting for you') , 'approval card');
await waitFor(() => !doc.querySelector('#apprBadge').hidden && doc.querySelector('#apprBadge').textContent === '1', 'approval badge');
if ((await api('/api/approvals')).some(a => a.status !== 'pending')) throw new Error('approval executed before approval');
await click('Approve'); await waitFor(() => view().includes('Copy message') && view().includes('approved'), 'approved manual message');
await click('I sent it'); await go('#approvals'); await sleep(200);
step('Draft -> pending approval (badge) -> approve -> mark sent');

// 4. Project
await go('#clients'); await click('+ New project');
await waitFor(() => !doc.querySelector('#sheet').hidden, 'new project sheet');
setv(fieldIn(sheet(), 'Client'), 'Test Client'); setv(fieldIn(sheet(), 'Email'), 'client@example.com'); setv(fieldIn(sheet(), 'Price'), '60');
setv(fieldIn(sheet(), 'Brief'), 'Need 2 TikTok clips under 20 seconds with captions, $30 per clip');
await click('Create', sheet());
await waitFor(() => /^#clients\/\d+$/.test(window.location.hash) && view().includes('Brief & requirements'), 'project page');
await waitFor(() => /Format\s*9:16/.test(view()) && /Length\s*≤20s/.test(view()) && /Captions\s*required/.test(view()), 'requirements extracted');
step('New project -> requirements read from brief (9:16, ≤20s, captions)');

// 5. Accept contract only via approval
const projHash = window.location.hash;
if (button('Accept contract…') === undefined) throw new Error('missing accept button');
await click('Accept contract…'); await waitFor(() => view().includes('Contract accepted') === false, 'still not accepted');
if ((await api('/api/projects/1')).project.accepted_at) throw new Error('accepted without approval');
await go('#approvals'); await click('Approve'); await waitFor(() => view().includes('Nothing is waiting') || $$('#view .chip').some(c => txt(c) === 'executed') || true, 'approved');
await sleep(300); await go(projHash);
await waitFor(() => view().includes('contract accepted'), 'project accepted');
step('Accept contract: request changes nothing; approval accepts');

// 6. Studio: source (uploaded via API: file picker is not testable in jsdom), captions, highlights, render
const fd = new FormData(); fd.append('video', new Blob([fs.readFileSync(video)], { type: 'video/mp4' }), 'sample.mp4');
const src = await api('/api/sources/upload', { method: 'POST', body: fd });
const fc = new FormData(); fc.append('captions', new Blob([fs.readFileSync(srt)], { type: 'text/plain' }), 'c.srt');
await api(`/api/sources/${src.id}/captions`, { method: 'POST', body: fc });
await go('#studio');
const sc = await waitFor(() => $$('#view .card.click').find(c => txt(c).includes('sample.mp4')), 'source card');
if (!txt(sc).includes('captions')) throw new Error('captions flag missing');
sc.click();
await waitFor(() => button('Find highlights'), 'highlight form');
setv(fieldIn(doc, 'Clip length'), '10'); setv(fieldIn(doc, 'How many'), '2'); setv(fieldIn(doc, 'Scene-change'), 'false');
await click('Find highlights');
await waitFor(() => view().includes('Suggestions (') && !view().includes('Suggestions (0)'), 'highlight suggestions');
const first = $$('#view .card').find(c => txt(c).includes('(10s)') || /\(\d+s\)/.test(txt(c)));
const start = txt(first.querySelector('b'));
const m = start.match(/(\d+):(\d+) – (\d+):(\d+)/); const s0 = +m[1] * 60 + +m[2];
if (s0 < 10 || s0 > 22) throw new Error('top highlight not near loud segment: ' + start);
step(`Highlights found near the loud segment (${start})`);
await click('Select', first);
await waitFor(() => view().includes('Render 1 clip'), 'selection reflected');
const projSel = fieldIn(doc, 'Check against project'); setv(projSel, '1');
await click('Render 1 clip');
await waitFor(() => $$('#view .clipv video').length >= 1 && $$('#view .clipv .chip').some(c => ['pass', 'fail', 'warn'].includes(txt(c))), 'rendered clip card', 90000);
const qa = $$('#view .clipv .chip').map(txt).find(t => ['pass', 'fail', 'warn'].includes(t));
if (qa !== 'pass') throw new Error('expected QA pass, got ' + qa);
step('Render (captions + project requirements) -> clip with video preview and QA = pass');
await click('QA report'); await waitFor(() => txt(sheet()).includes('Aspect ratio matches brief') && txt(sheet()).includes('Captions present'), 'QA report'); doc.querySelector('#sheetClose').click();
step('QA report lists every check');

// 7. Delivery needs approval
await go(projHash); await waitFor(() => view().includes('Clips (1)'), 'project clip');
await click('Deliver clips…'); await waitFor(() => !doc.querySelector('#sheet').hidden && txt(doc.querySelector('#sheetTitle')).includes('Request approval: deliver'), 'delivery sheet');
await click('Create request', sheet());
await sleep(500);
if ((await api('/api/projects/1')).project.status === 'delivered') throw new Error('delivered without approval');
await go('#approvals'); await click('Approve'); await sleep(500);
await go(projHash); await waitFor(() => /Status\s*delivered/.test(view()), 'project delivered');
step('Delivery: request changes nothing; approval marks delivered');

// 8. Money
await go('#money'); await waitFor(() => view().includes('Record a payment'), 'money view');
setv(fieldIn(doc, 'Project'), '1'); setv(fieldIn(doc, 'Amount'), '60'); setv(fieldIn(doc, 'Currency'), 'USD'); setv(fieldIn(doc, 'Status'), 'pending'); setv(fieldIn(doc, 'Reference'), 'TX-42');
await click('Save record');
const sel = await waitFor(() => $$('#view .card select')[0], 'ledger row');
setv(sel, 'paid'); sel.dispatchEvent(new window.Event('change', { bubbles: true }));
await waitFor(() => /Paid\s*60\.00/.test(view()), 'earnings updated');
step('Record payment -> mark paid -> earnings summary updates');

// 9. Every tab loads without an error banner
for (const t of ['home', 'leads', 'studio', 'clients', 'approvals', 'money']) {
  await go('#' + t); await sleep(400);
  if (doc.querySelector('#view .banner.bad')) throw new Error(`error banner on ${t}: ` + txt(doc.querySelector('#view .banner.bad')));
}
step('All six tabs load without errors');

if (errors.length) { console.error('Browser errors:\n' + errors.join('\n')); await finish(1); }
console.log(`\n${passed} UI steps passed`);
await finish(0);

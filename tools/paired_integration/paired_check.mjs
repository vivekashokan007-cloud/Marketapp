// R8 (Codex R7 s5): paired integration check at EXACT native + PWA SHAs.
// Usage: MARKETAPP_DIR=<Marketapp worktree> MARKETVIVI_DIR=<MarketVivi worktree> node paired_check.mjs <out.json>
// Real pieces: the injected window.NativeBridge facade extracted from MainActivity.kt, the PWA functions from
// app.js, and the Python brain/store/daily review.  STAND-IN: the Kotlin SingleLegAdmissionCoordinator is not
// executed here (no JVM/Chaquopy); a JS AndroidBridge emulates its PENDING -> status contract and calls the
// real Python. The coordinator's own behaviour is covered by SingleLegAdmissionCoordinatorTest (JVM).
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { runFacade } from '../bridge_facade/facade.mjs';

const A = path.resolve(process.env.MARKETAPP_DIR || '.');
const V = path.resolve(process.env.MARKETVIVI_DIR || '../MarketVivi');
const sha = d => execFileSync('git', ['-C', d, 'rev-parse', 'HEAD']).toString().trim();
const PYH = path.join(A, 'tools', 'paired_integration', 'paired_py.py');
const tmp = mkdtempSync(path.join(os.tmpdir(), 'paired-'));
const sl = path.join(tmp, 'single_leg_v1');
const py = (...args) => execFileSync('python3', [PYH, ...args], { cwd: A }).toString().trim();

const checks = [];
const check = (name, fn) => { try { fn(); checks.push({ name, ok: true }); } catch (e) { checks.push({ name, ok: false, error: String(e.message || e) }); } };

const resultJson = py('setup', sl);
const resultPath = path.join(tmp, 'result.json');
writeFileSync(resultPath, resultJson);
const result = JSON.parse(resultJson);

// --- AndroidBridge stand-in (emulates the coordinator contract; calls the REAL Python) ---
let capital = null;
const pending = {};
let seq = 0;
const AndroidBridge = {
  getSingleLegPaperEnabled: () => true,
  setSingleLegPaperEnabled: () => undefined,
  getIndexSelector: () => 'BOTH',
  setIndexSelector: s => s,
  getSingleLegCapital: () => JSON.stringify(capital == null ? { status: 'UNSET' } : { status: 'SET', capital }),
  setSingleLegCapital: v => { if (!(v > 0)) return JSON.stringify({ status: 'REJECTED', reason: 'CAPITAL_CONFIG_INVALID' }); capital = v; return JSON.stringify({ status: 'OK', capital: v }); },
  singleLegAdmit: ix => { const id = `pair-${++seq}`; pending[id] = ix; return JSON.stringify({ status: 'PENDING', request_id: id, index: ix }); },
  singleLegAdmitStatus: id => {
    if (pending[id]) { delete pending[id]; const o = JSON.parse(py('admit', sl, id, capital == null ? 'null' : String(capital), resultPath)); return JSON.stringify({ ...o, completed: true }); }
    // unknown id (stand-in for a restarted coordinator): definitive reconcile under the admission lock
    const c = JSON.parse(py('cancel', sl, id));
    return JSON.stringify(c.found ? { ...c, completed: true, reconciled: true } : { status: 'PENDING', phase: 'OUTCOME_UNCONFIRMED', request_id: id });
  },
  singleLegRead: () => py('read', sl),
  dailyReviewLatest: () => '{}',
};
const ctx = vm.createContext({ AndroidBridge });
const NB = runFacade(ctx);

// --- real PWA functions ---
const src = readFileSync(path.join(V, 'app.js'), 'utf8');
const fn = name => { const m = new RegExp(`^function ${name}\\(`, 'm').exec(src); assert.ok(m, `app.js defines ${name}`); return src.slice(m.index, src.indexOf('\n}\n', m.index) + 3); };
const FNS = ['getIndexSelector', 'iaEsc', 'singleLegEnabled', 'loadSingleLegStore', 'admitSingleLeg', 'pollSingleLegAdmit',
  'finishSingleLegAdmit', 'saveSingleLegPending', 'resumeSingleLegPending', 'singleLegCapital', 'renderSingleLegCard', 'reviewNet', 'renderReviewGroups', 'renderDailyReviewCard'];
const alerts = [];
const timers = [];
const pctx = vm.createContext({ JSON, String, Object, Array, Math, Intl, Date, Number, window: { NativeBridge: NB }, alert: m => alerts.push(m),
  setTimeout: f => timers.push(f), localStorage: (() => { const m = {}; return { getItem: k => m[k] ?? null, setItem: (k, v) => { m[k] = v; } }; })() });
vm.runInContext(`const INDEX_SELECTOR_KEY='mr_index_selector_v1'; let DAILY_REVIEW_CACHE=null; let SINGLE_LEG_STORE=null; const SINGLE_LEG_PENDING_KEY='mr_single_leg_pending_v1'; let SINGLE_LEG_PENDING={}; let SINGLE_LEG_POLLING={};\n${FNS.map(fn).join('\n')}`, pctx);
const drain = () => { let n = 0; while (timers.length && n++ < 20) timers.shift()(); };

// 1. bridge surface: every NativeBridge method the PWA calls for single-leg / daily review exists in the facade
const used = [...new Set([...src.matchAll(/NativeBridge\??\.(\w+)/g)].map(m => m[1]))];
const scoped = used.filter(n => /singleLeg|SingleLeg|dailyReview|IndexSelector|indexAuthority/.test(n));
check('facade exposes every single-leg/daily-review method app.js calls', () => assert.deepEqual(scoped.filter(n => typeof NB[n] !== 'function'), []));
const missingAll = used.filter(n => typeof NB[n] !== 'function');

// 2. card renders the Python-published proposal
check('single-leg card renders the published Python plan and capital-unset warning', () => {
  const html = pctx.renderSingleLegCard(result);
  assert.match(html, /admitSingleLeg\('NF'\)/);
  assert.match(html, /not set/);
});
// 3. admission with capital UNSET -> named rejection from Python, surfaced
check('capital unset -> CAPITAL_CONFIG_UNAVAILABLE via PENDING/status poll', () => {
  const p = pctx.admitSingleLeg('NF'); assert.equal(p.status, 'PENDING'); drain();
  assert.match(alerts.at(-1) || '', /CAPITAL_CONFIG_UNAVAILABLE/);
});
// 4. capital set via the facade, then admission succeeds and the store read renders the position
check('capital set -> ADMITTED; store read renders the simulated position', () => {
  assert.equal(JSON.parse(NB.setSingleLegCapital(250000)).status, 'OK');
  const n = alerts.length; pctx.admitSingleLeg('NF'); drain();
  assert.equal(alerts.length, n, alerts.at(-1));
  const html = pctx.renderSingleLegCard(result);
  assert.match(html, /Local Paper positions/);
  assert.match(html, /simulated fill \(broker snapshot\)/);
  assert.match(html, /Capital: ₹250000/);
});
// 5. reconcile by request id through the facade
check('request status reconciles by id after completion', () => {
  const st = JSON.parse(NB.singleLegAdmitStatus('pair-2'));
  assert.equal(st.found, true); assert.equal(st.status, 'ADMITTED');
});
// 5b. unknown request after a restart is fenced (definitive) and the fence blocks a later admission for that id
check('restart: unknown id is fenced under the lock; later admit with that id cannot commit', () => {
  const st = JSON.parse(NB.singleLegAdmitStatus('pair-lost'));
  assert.equal(st.completed, true); assert.equal(st.reason, 'CANCELLED_BEFORE_COMMIT');
  const late = JSON.parse(py('admit', sl, 'pair-lost', '250000', resultPath));
  assert.equal(late.reason, 'CANCELLED_BEFORE_COMMIT'); assert.equal(late.replayed, true);
});
// 6. daily review report from the same store -> groups rendered per index
let review = null;
check('daily review groups (Python) render in the PWA card; BNF independent; no combined score', () => {
  review = JSON.parse(py('review', sl, resultPath));
  const g = review.by_index.NF.paper_outcome_groups['ACTUAL_PAPER|index_single_leg_v1'].groups;
  assert.equal(g.SINGLE_LEG_BUY.entries, 1);
  assert.equal(g.SINGLE_LEG_SELL.net_status, 'NO_OBSERVATIONS');
  vm.runInContext(`DAILY_REVIEW_CACHE = ${JSON.stringify({ latest: review })};`, pctx);
  const html = pctx.renderDailyReviewCard();
  assert.match(html, /SINGLE_LEG_BUY: entries 1/);
  assert.match(html, /SINGLE_LEG_SELL: entries 0/);
  assert.equal(review.by_index.BNF.paper_outcome_groups['ACTUAL_PAPER|index_single_leg_v1'].groups.SINGLE_LEG_BUY.entries, 0);
  assert.doesNotMatch(JSON.stringify(Object.keys(review)), /combined/i);
});

const out = {
  schema: 'mr.paired_integration.v1', generated_at: new Date().toISOString(),
  marketapp: { dir: A, sha: sha(A) }, marketvivi: { dir: V, sha: sha(V) },
  real: ['MainActivity.kt injected facade', 'app.js single-leg + daily review functions', 'Python brain/single_leg_store_v1/daily_review_v1'],
  stand_in: ['Kotlin SingleLegAdmissionCoordinator + NativeBridge prefs (JS AndroidBridge emulation; covered separately by JVM tests)',
             'clock fixed by fixture; no quote fetch (fixture snapshot); no network'],
  checks, pwa_nativebridge_methods_not_in_facade_informational: missingAll,
  pass: checks.every(c => c.ok),
};
writeFileSync(process.argv[2] || path.join(tmp, 'paired.json'), JSON.stringify(out, null, 2));
console.log(JSON.stringify({ pass: out.pass, marketapp: out.marketapp.sha, marketvivi: out.marketvivi.sha, checks: checks.map(c => `${c.ok ? 'ok' : 'FAIL'} ${c.name}${c.error ? ': ' + c.error : ''}`) }, null, 1));
process.exit(out.pass ? 0 : 1);

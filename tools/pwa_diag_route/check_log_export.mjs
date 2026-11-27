// R2 (Codex R1 deliverable 4): proves, against the EXACT pinned PWA file, that the owner's Logs-tab export
// carries the native recovery diagnostics. Loads MarketVivi/log-viewer.js unmodified in a VM with a stub DOM and a
// NativeBridge whose getLogBuffer returns what the native side now appends (RECOVERY_DIAG_SNAPSHOT,
// STORAGE_BREAKDOWN, RAW_OUTBOX_STATS, CLOUD_PASS_STATS), then calls the same buildExportSnapshot() the
// "CSV file" / "TXT file" / Share buttons use. Usage: MARKETVIVI_DIR=<worktree> node check_log_export.mjs
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';

const V = path.resolve(process.env.MARKETVIVI_DIR || '../MarketVivi');
const file = path.join(V, 'log-viewer.js');
const src = readFileSync(file, 'utf8');
const head = execFileSync('git', ['-C', V, 'rev-parse', 'HEAD']).toString().trim();
const blob = execFileSync('git', ['-C', V, 'rev-parse', 'HEAD:log-viewer.js']).toString().trim();

const any = () => new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => '' : k === 'style' || k === 'classList' || k === 'dataset' ? any() : any()), apply: () => any(), set: () => true });
const document = { readyState: 'complete', getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
  createElement: () => any(), head: any(), body: any(), addEventListener: () => {} };
const entries = [
  { ts: 1795777000000, level: 'I', tag: 'CloudRecovery', msg: 'RECOVERY_DIAG_SNAPSHOT {"schema":"mr.recovery_diag.v1","revision":40,"last_pass":{"mode":"DEFER","budget":"DEFER_COOLDOWN_412s","bytes_hashed":0,"commit_attempted":false,"commit_confirmed":false}}' },
  { ts: 1795776990000, level: 'I', tag: 'CloudRecovery', msg: 'STORAGE_BREAKDOWN root=files files=11926 bytes=15216000000 truncated=false failures=0 incomplete=no elapsed_ms=812 research_m0=9000f/12000000000b' },
  { ts: 1795776990001, level: 'I', tag: 'CloudRecovery', msg: 'RAW_OUTBOX_STATS metadata=0 bodies=0 other=0 bytes=0 acknowledged=0 pending=0 orphan_acks=0 ack_bytes=0 truncated=false' },
  { ts: 1795776980000, level: 'I', tag: 'CloudRecovery', msg: 'CLOUD_PASS_STATS trigger=worker mode=FULL gate=MUTATION_SIGNAL budget=RUN outcome=VERIFIED_CHECKPOINT commit_attempted=true commit_confirmed=true total_ms=166000 writers=c3/rev2 classes=files/cloud_history_outbox_v1/H_H.bin=2 prefkeys=cloud_web_cache_v1:storage' },
];
let filterSeen = 'unset';
const NativeBridge = { getLogBuffer: f => { filterSeen = f; return JSON.stringify(entries); }, getLogCaptureMode: () => 'LOGCAT' };
const window = { NativeBridge, addEventListener: () => {}, location: { href: 'https://example.invalid/' } };
const ctx = vm.createContext({ window, document, navigator: {}, console, setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {}, Date, JSON, Math, URL, Blob: class {} });
vm.runInContext(src, ctx, { filename: 'log-viewer.js' });
const LV = window.LogViewer;
assert.ok(LV && typeof LV.buildExportSnapshot === 'function', 'pinned log-viewer.js exposes the export used by the buttons');
assert.ok(/onClick\('lvExportCsv', \(\) => this\.exportFile\('csv'\)\)/.test(src) && /const snapshot = this\.buildExportSnapshot\(\);/.test(src), 'CSV button -> exportFile -> buildExportSnapshot');
LV.state.filter = 'ALL';
const snap = LV.buildExportSnapshot();
assert.equal(filterSeen, null, 'ALL filter passes null: native returns every tag');
for (const needle of ['RECOVERY_DIAG_SNAPSHOT', 'STORAGE_BREAKDOWN root=files', 'RAW_OUTBOX_STATS', 'commit_confirmed=true', 'incomplete=no'])
  { assert.ok(snap.csv.includes(needle), `CSV contains ${needle}`); assert.ok(snap.txt.includes(needle), `TXT contains ${needle}`); }
assert.equal(snap.entries.length, entries.length);
const out = { schema: 'mr.pwa_diag_route.v1', marketvivi_head: head, log_viewer_blob: blob,
  log_viewer_sha256: crypto.createHash('sha256').update(src).digest('hex'), csv_bytes: snap.csv.length, pass: true };
console.log(JSON.stringify(out));

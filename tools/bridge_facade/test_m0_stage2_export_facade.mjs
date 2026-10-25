// M0 Stage 2 CI-2: the REAL MainActivity facade forwards the read-only export methods and degrades explicitly.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { extractFacadeJs, runFacade } from './facade.mjs';

function ctxWith(ab) { const ctx = { console: { log() {} }, JSON, AndroidBridge: ab }; ctx.window = ctx; vm.createContext(ctx); runFacade(ctx, extractFacadeJs()); return ctx; }

test('facade forwards export / result / cancel with a string identity only', () => {
  const calls = [];
  const ctx = ctxWith({ exportResearchEvidence: (s) => (calls.push(['e', s]), '{"status":"PICKER_OPENING"}'),
    getResearchExportResult: () => (calls.push(['r']), '{"status":"NONE"}'), cancelResearchExport: () => (calls.push(['c']), '{}') });
  assert.equal(JSON.parse(ctx.NativeBridge.exportResearchEvidence('{"version":"2.6.83"}')).status, 'PICKER_OPENING');
  ctx.NativeBridge.exportResearchEvidence({ not: 'a string' });
  ctx.NativeBridge.getResearchExportResult(); ctx.NativeBridge.cancelResearchExport();
  assert.deepEqual(calls, [['e', '{"version":"2.6.83"}'], ['e', ''], ['r'], ['c']]);
});

test('missing native export methods -> explicit NATIVE_METHOD_ABSENT', () => {
  const ctx = ctxWith({});
  for (const m of ['exportResearchEvidence', 'getResearchExportResult', 'cancelResearchExport'])
    assert.equal(JSON.parse(ctx.NativeBridge[m]('')).reason, 'NATIVE_METHOD_ABSENT');
});

test('native declares each export method as @JavascriptInterface', () => {
  const nb = readFileSync(new URL('../../app/src/main/java/com/marketradar/app/NativeBridge.kt', import.meta.url), 'utf8');
  for (const m of ['exportResearchEvidence', 'getResearchExportResult', 'cancelResearchExport'])
    assert.match(nb, new RegExp(`@JavascriptInterface\\s+fun ${m}\\(`));
});

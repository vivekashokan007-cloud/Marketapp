// M0 Batch 1 W1: the REAL MainActivity.injectNativeBridge() facade forwards the research collector methods to
// AndroidBridge (boolean preserved, return value passed through) and degrades to an explicit unavailable state
// when the native method is absent.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { extractFacadeJs, runFacade } from './facade.mjs';

function ctxWith(ab) { const ctx = { console: { log() {} }, JSON, AndroidBridge: ab }; ctx.window = ctx; vm.createContext(ctx); runFacade(ctx, extractFacadeJs()); return ctx; }

test('facade forwards research status and enable', () => {
  const calls = [];
  const ctx = ctxWith({ getResearchCollectorStatus: () => (calls.push('get'), '{"label":"DISABLED"}'),
    setResearchCollectorEnabled: (on) => (calls.push(on), '{"label":"ENABLED_NOT_CAPTURING"}') });
  assert.equal(ctx.NativeBridge.getResearchCollectorStatus(), '{"label":"DISABLED"}');
  assert.equal(ctx.NativeBridge.setResearchCollectorEnabled(true), '{"label":"ENABLED_NOT_CAPTURING"}');
  ctx.NativeBridge.setResearchCollectorEnabled('yes');   // non-boolean never becomes "on"
  assert.deepEqual(calls, ['get', true, false]);
});

test('facade with an AndroidBridge lacking the methods returns explicit unavailable, no throw', () => {
  const ctx = ctxWith({});
  assert.deepEqual(JSON.parse(ctx.NativeBridge.getResearchCollectorStatus()), { available: false, label: 'UNAVAILABLE', reason: 'NATIVE_METHOD_ABSENT' });
  assert.equal(JSON.parse(ctx.NativeBridge.setResearchCollectorEnabled(true)).reason, 'NATIVE_METHOD_ABSENT');
});

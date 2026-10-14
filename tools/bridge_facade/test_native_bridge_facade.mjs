// b506 (Codex option A) regression tests 1, 2, 4 and 5: they execute the REAL injected facade from MainActivity.kt.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extractFacadeJs, kotlinEvMethods, runFacade } from './facade.mjs';

const EXPECTED_EV_COUNT = 22;   // reviewed API count; a change must be reported and reviewed, not silently adjusted

/** AndroidBridge spy: records every call; returns per-method programmed values. */
function spy(returns = {}) {
  const calls = [];
  const target = {};
  const ab = new Proxy(target, {
    get(_t, name) {
      if (typeof name !== 'string') return undefined;
      return (...args) => { calls.push({ name, args }); return name in returns ? returns[name](...args) : `RET:${name}`; };
    },
  });
  return { ab, calls };
}

test('1. generic coverage: every Kotlin @JavascriptInterface ev* method is exposed by the real facade', () => {
  const kt = kotlinEvMethods();
  assert.equal(kt.length, EXPECTED_EV_COUNT, `ev API count changed: ${kt.map(m => m.name).join(',')}`);
  const { ab } = spy();
  const nb = runFacade({ AndroidBridge: ab });
  const missing = kt.filter(m => typeof nb[m.name] !== 'function').map(m => m.name);
  assert.deepEqual(missing, []);
  for (const m of kt) assert.equal(nb[m.name].length, m.params.length, `${m.name} arity`);
  // nothing ev-shaped is exposed that native does not declare
  const extra = Object.keys(nb).filter(k => /^ev[A-Z]/.test(k) && !kt.some(m => m.name === k));
  assert.deepEqual(extra, []);
  console.log(`# ${kt.length - missing.length}/${kt.length} ev methods exported by the real facade`);
});

test('1b. a method added in Kotlin without a wrapper fails coverage', () => {
  const kt = kotlinEvMethods() .concat([{ name: 'evFutureMethod', params: [] }]);
  const nb = runFacade({ AndroidBridge: spy().ab });
  assert.ok(kt.some(m => typeof nb[m.name] !== 'function'));
});

test('2. forwarding contract: same method once, args in order, return preserved; injection calls nothing', () => {
  const kt = kotlinEvMethods();
  const json = '{"a":"q\\"uote","nl":"line1\nline2","n":0,"f":false}';
  const values = [json, 'req-7f3a', 'trade:42', '', '0', 'false', 'ev12:NF:2026-10-15:0123456789abcdef'];
  let forwarded = 0;
  for (const m of kt) {
    for (const ret of m.returns === 'Boolean' ? [true, false] : [json, '', 'OK']) {
      const { ab, calls } = spy({ [m.name]: () => ret });
      const nb = runFacade({ AndroidBridge: ab });
      assert.deepEqual(calls.filter(c => c.name.startsWith('ev')), [], 'injection must not invoke ev methods');
      const before = calls.length;
      const args = m.params.map((_, i) => values[(i + kt.indexOf(m)) % values.length]);
      const got = nb[m.name](...args);
      const mine = calls.slice(before);
      assert.equal(mine.length, 1, `${m.name} called once`);
      assert.equal(mine[0].name, m.name, `${m.name} forwards to the same method`);
      assert.deepEqual(mine[0].args, args, `${m.name} args in order`);
      assert.strictEqual(got, ret, `${m.name} return preserved`);
    }
    forwarded++;
  }
  assert.equal(forwarded, EXPECTED_EV_COUNT);
  console.log(`# ${forwarded}/${EXPECTED_EV_COUNT} ev methods correctly forwarded through the real facade`);
});

test('4. lifecycle paths through the real facade (prepare/get/confirm/cancel/mark, recovery, Rule B, manual close)', () => {
  const { ab, calls } = spy({
    evPrepareAcceptance: (d, id) => JSON.stringify({ request_id: 'r1', session_date: d, decision_id: id }),
    evGetAcceptance: (r) => JSON.stringify({ request_id: r, status: 'READY', reservation_id: 'res1' }),
    evConfirmAcceptance: (id, res) => JSON.stringify({ status: 'CONFIRMED', decision_id: id, reservation_id: res }),
    evCancelAcceptance: (id, res) => JSON.stringify({ status: 'CANCELLED', reservation_id: res }),
    evMarkTradePersisted: (id, t, u) => JSON.stringify({ status: 'PERSISTED', trade_id: t, trade_uid: u }),
    evAcceptanceRecovery: () => '[]',
    evAcceptanceReconciledClosed: (id) => `CLOSED:${id}`,
    evAcceptanceQuarantine: () => '{"quarantined":[]}',
    evRuleBPending: () => '[{"trade_id":"t1"}]',
    evRuleBJournal: (t, p) => (JSON.parse(p).status === 'CLOSED' ? 'JOURNALED' : 'REFUSED'),
    evRuleBRemoteFailed: () => true,
    evRuleBAckState: (t, st) => st === 'REMOTE_CONFIRMED',
    evRuleBAck: () => false,                                     // deprecated: native stays fail-closed
    evNoteManualClose: () => 'MANUAL_PENDING',
    evManualCloseRequest: () => 'MANUAL_PENDING',
    evManualIntentAbandon: () => 'WITHDRAWN',
    evManualCloseJournal: () => 'ALREADY_JOURNALED',
    evManualCloseResult: (t, r) => (r === 'MANUAL_WON' ? 'REMOTE_CONFIRMED' : 'REFUSED'),
  });
  const nb = runFacade({ AndroidBridge: ab });
  const p = JSON.parse(nb.evPrepareAcceptance('2026-10-15', 'ev12:NF:2026-10-15:0123456789abcdef'));
  assert.equal(p.decision_id, 'ev12:NF:2026-10-15:0123456789abcdef');
  assert.equal(JSON.parse(nb.evGetAcceptance(p.request_id)).reservation_id, 'res1');
  assert.equal(JSON.parse(nb.evConfirmAcceptance(p.decision_id, 'res1')).status, 'CONFIRMED');
  assert.equal(JSON.parse(nb.evCancelAcceptance(p.decision_id, 'res1')).status, 'CANCELLED');
  assert.equal(JSON.parse(nb.evMarkTradePersisted(p.decision_id, '9001', 'uid-1')).trade_uid, 'uid-1');
  assert.equal(nb.evAcceptanceRecovery(), '[]');
  assert.equal(nb.evAcceptanceReconciledClosed('d1'), 'CLOSED:d1');
  assert.equal(nb.evAcceptanceQuarantine(), '{"quarantined":[]}');
  assert.equal(JSON.parse(nb.evRuleBPending())[0].trade_id, 't1');
  assert.equal(nb.evRuleBJournal('t1', '{"status":"CLOSED","exit":"line\\n"}'), 'JOURNALED');
  assert.strictEqual(nb.evRuleBRemoteFailed('t1', 'http 503'), true);
  assert.strictEqual(nb.evRuleBAckState('t1', 'REMOTE_CONFIRMED'), true);
  assert.strictEqual(nb.evRuleBAckState('t1', 'BOGUS'), false);
  assert.strictEqual(nb.evRuleBAck('t1'), false);              // forwarding keeps the legacy refusal
  assert.equal(nb.evNoteManualClose('t1', 'd1'), 'MANUAL_PENDING');
  assert.equal(nb.evManualCloseRequest('t1', 'd1', '{"status":"CLOSED"}'), 'MANUAL_PENDING');
  assert.equal(nb.evManualIntentAbandon('t1'), 'WITHDRAWN');
  assert.equal(nb.evManualCloseJournal('t1', '{"status":"CLOSED"}'), 'ALREADY_JOURNALED');
  assert.equal(nb.evManualCloseResult('t1', 'MANUAL_WON'), 'REMOTE_CONFIRMED');
  assert.equal(nb.evManualCloseResult('t1', 'RULE_B_WON'), 'REFUSED');
  assert.ok(calls.every(c => c.name.startsWith('ev')));
});

test('5. facade adds forwarders only: no raw AndroidBridge exposure, existing methods intact, injected once', () => {
  const js = extractFacadeJs();
  assert.doesNotMatch(js, /window\.AndroidBridge\s*=|NativeBridge\s*=\s*AndroidBridge|new Proxy/);
  const { ab, calls } = spy();
  const ctx = { AndroidBridge: ab };
  const nb = runFacade(ctx);
  for (const k of ['isNative', 'startMarketService', 'setOpenTrades', 'getLogBuffer', 'init']) assert.equal(typeof nb[k], 'function', k);
  assert.equal(nb.isNative(), true);
  runFacade(ctx);                                                // guarded by window._nativeBridgeInjected
  assert.strictEqual(ctx.NativeBridge, nb);
  assert.deepEqual(calls.filter(c => c.name.startsWith('ev')), []);
});

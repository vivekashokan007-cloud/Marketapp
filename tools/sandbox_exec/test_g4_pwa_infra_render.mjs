// G4: render the exact Android infra-status outputs (golden from ExecutionInfraStatusG4Test) through the REAL
// "Execution Infra State" template of the paired MarketVivi app.js. Configured-but-unverified live must not read READY.
// Usage: MARKETVIVI_DIR=/path/to/MarketVivi node --test tools/sandbox_exec/test_g4_pwa_infra_render.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const golden = JSON.parse(readFileSync(join(here, 'g4_infra_status_golden.json'), 'utf8'));
const viviDir = process.env.MARKETVIVI_DIR;
assert.ok(viviDir, 'MARKETVIVI_DIR must point at the paired MarketVivi checkout');
const app = readFileSync(join(viviDir, 'app.js'), 'utf8');

function infraTemplate() {
  const start = app.indexOf('Instrument keys: <b>${infra.instrumentKeyPresentRows');
  const errAt = app.indexOf("${infra.error ? ", start);
  const end = app.indexOf('\n', errAt);
  assert.ok(start > 0 && errAt > start && end > errAt, 'Execution Infra State template not found in app.js');
  const body = app.slice(start, end);
  assert.ok(body.includes('Live <b>${infra.liveReady ?'), 'template still renders Live from liveReady');
  // eslint-disable-next-line no-new-func
  return new Function('infra', 'return `' + body + '`;');
}
const render = infraTemplate();
const text = (infra) => render(infra).replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();

test('token + HTTPS proxy only: proxy shown CONFIGURED, Live not READY, Paper READY', () => {
  const t = text(golden.token_and_https_proxy_only);
  assert.match(t, /Proxy: CONFIGURED/);
  assert.match(t, /Live WAIT/);
  assert.doesNotMatch(t, /Live READY/);
  assert.match(t, /Paper READY/);
});

test('absent configuration: Proxy NOT SET, Live WAIT, Paper READY', () => {
  const t = text(golden.absent_configuration);
  assert.match(t, /Token: MISSING/);
  assert.match(t, /Proxy: NOT SET/);
  assert.match(t, /Live WAIT/);
  assert.match(t, /Paper READY/);
});

test('sandbox and exception outputs render compatibly', () => {
  assert.match(text(golden.sandbox_configured), /Sandbox READY/);
  const e = text(golden.bridge_exception);
  assert.match(e, /Error: boom/);
  assert.match(e, /Paper READY/);
  assert.match(e, /Live WAIT/);
});

test('sensitivity: the pre-G4 status (liveReady = token && proxy) WOULD have rendered Live READY', () => {
  const legacy = { ...golden.token_and_https_proxy_only, liveReady: true };
  assert.match(text(legacy), /Live READY/);
});

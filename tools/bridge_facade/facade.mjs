// Extracts and executes the REAL injected window.NativeBridge facade from MainActivity.kt (b506, Codex option A).
// Used by this repo's tests and by MarketVivi's PWA-chain test (which checks out Marketapp).
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = process.env.MARKETAPP_DIR
  ? path.resolve(process.env.MARKETAPP_DIR)
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SRC = path.join(ROOT, 'app', 'src', 'main', 'java', 'com', 'marketradar', 'app');

export function mainActivitySource() { return readFileSync(path.join(SRC, 'MainActivity.kt'), 'utf8'); }
export function nativeBridgeSource() { return readFileSync(path.join(SRC, 'NativeBridge.kt'), 'utf8'); }

/** The JS literal of injectNativeBridge(): the raw string between `val js = """` and `""".trimIndent()`. */
export function extractFacadeJs(src = mainActivitySource()) {
  const fn = src.indexOf('private fun injectNativeBridge()');
  if (fn < 0) throw new Error('injectNativeBridge not found');
  const start = src.indexOf('val js = """', fn);
  const end = src.indexOf('""".trimIndent()', start);
  if (start < 0 || end < 0) throw new Error('facade raw string not found');
  let js = src.slice(start + 'val js = """'.length, end);
  if (/\$\{(?!'\$'\})|\$[A-Za-z_]/.test(js)) throw new Error('unexpected Kotlin string template in facade');
  return js.replace(/\$\{'\$'\}/g, '$');
}

/** Kotlin @JavascriptInterface ev* declarations: [{name, params:[...]}], in source order. */
export function kotlinEvMethods(src = nativeBridgeSource()) {
  const out = [];
  for (const m of src.matchAll(/@JavascriptInterface\s*\n\s*fun (ev\w+)\(([^)]*)\)\s*:\s*(\w+)/g)) {
    out.push({ name: m[1], params: m[2].split(',').map(p => p.trim()).filter(Boolean).map(p => p.split(':')[0].trim()),
               returns: m[3] });
  }
  return out;
}

/**
 * Runs the extracted facade in `ctx` (a vm context or plain object) whose global AndroidBridge is supplied by the
 * caller; returns ctx.NativeBridge (window === the context global, as in the WebView).
 */
export function runFacade(ctx, js = extractFacadeJs()) {
  if (!vm.isContext(ctx)) vm.createContext(ctx);
  ctx.window = ctx;
  if (!ctx.console) ctx.console = { log() {}, warn() {}, error() {} };
  vm.runInContext(js, ctx);
  return ctx.NativeBridge;
}

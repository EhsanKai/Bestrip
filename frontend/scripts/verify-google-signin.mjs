// Focused runtime checks for the Google Sign-In frontend integration.
// No real network, no Google endpoint, no OAuth token, no provider object,
// no browser storage is accessed - everything here runs against the real
// TypeScript source through an isolated `vm` context with mocked globals,
// the same technique frontend/scripts/verify-analytics.mjs already uses.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
const root = fileURLToPath(new URL('../src/', import.meta.url));
const read = path => fs.readFileSync(root + path, 'utf8');

// Values crossing the vm boundary come from a different realm (a distinct
// Object.prototype), so assert.deepEqual's prototype check fails even on
// identical plain data - compare by serialized shape instead.
function sameShape(actual, expected, message) {
  assert.equal(JSON.stringify(actual), JSON.stringify(expected), message);
}

function moduleFrom(source, globals, require = () => ({})) {
  const exports = {};
  const js = ts.transpileModule(source.replaceAll('import.meta.env', '__env'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  vm.runInNewContext(js, { exports, require, ...globals });
  return exports;
}

/* ------------------------------------------------------------------ */
/* lib/googleAuthReturn.ts - parsing the backend's plain query params  */
/* ------------------------------------------------------------------ */
function loadReturnModule(href = 'https://detoura.example/') {
  const calls = [];
  const win = { location: new URL(href), history: { replaceState: (_s, _t, url) => calls.push(url) } };
  const mod = moduleFrom(read('lib/googleAuthReturn.ts'), { window: win, URL, URLSearchParams });
  return { mod, calls };
}

const { mod: g } = loadReturnModule();

sameShape(g.readGoogleReturnOutcome('?google_auth=success'), { kind: 'success' });
sameShape(g.readGoogleReturnOutcome('?google_auth=error&reason=missing_parameters'), { kind: 'error', reason: 'cancelled' });
sameShape(g.readGoogleReturnOutcome('?google_auth=error&reason=failed'), { kind: 'error', reason: 'failed' });
sameShape(g.readGoogleReturnOutcome('?google_auth=error&reason=link_conflict'), { kind: 'error', reason: 'link_conflict' });
// A future backend reason this frontend has never seen must degrade safely,
// never crash and never be echoed back to the user verbatim.
sameShape(g.readGoogleReturnOutcome('?google_auth=error&reason=some_future_backend_reason'), { kind: 'error', reason: 'unknown' });
assert.equal(g.readGoogleReturnOutcome('?nothing=here'), null);
assert.equal(g.readGoogleReturnOutcome(''), null);
sameShape(
  g.readGoogleReturnOutcome('?google_link_required=1&link_id=glink_abcDEF123-_'),
  { kind: 'link_required', linkId: 'glink_abcDEF123-_' },
);
// A malformed/injected link_id is never trusted through to a UI state that
// would let the frontend later POST it to /auth/google/link/confirm.
for (const bad of ['<script>alert(1)</script>', 'javascript:alert(1)', '../../etc/passwd', '', 'a'.repeat(200)]) {
  sameShape(
    g.readGoogleReturnOutcome(`?google_link_required=1&link_id=${encodeURIComponent(bad)}`),
    { kind: 'error', reason: 'unknown' },
    `rejected as: ${bad}`,
  );
}
// Never surfaces a raw OAuth code/state/id_token even if one is present
// alongside the outcome params (defense in depth - the backend's own
// redirect never actually includes these, but the parser must not care).
const withSecrets = g.readGoogleReturnOutcome('?google_auth=success&code=SECRETCODE&state=SECRETSTATE&id_token=SECRETTOKEN');
assert(!JSON.stringify(withSecrets).includes('SECRET'));

// clearGoogleReturnParams: strips only the Google-return keys, leaves any
// other query untouched, and is a no-op (no history churn) when there is
// nothing to clear.
{
  const { mod, calls } = loadReturnModule('https://detoura.example/?google_auth=success&other=keep');
  mod.clearGoogleReturnParams();
  assert.equal(calls.length, 1);
  assert(!calls[0].includes('google_auth'));
  assert(calls[0].includes('other=keep'));
}
{
  const { mod, calls } = loadReturnModule('https://detoura.example/?other=keep');
  mod.clearGoogleReturnParams();
  assert.equal(calls.length, 0, 'nothing to clear must not touch history');
}

/* ------------------------------------------------------------------ */
/* api/client.ts - googleAuthStartUrl() and googleLinkConfirm()        */
/* ------------------------------------------------------------------ */
class FakeDetouraApiError extends Error {
  constructor(message, status, issue) {
    super(message);
    this.status = status;
    this.issue = issue;
  }
}
function loadClient({ csrfCookie = 'csrf-abc', status = 200, body = { ok: true }, base } = {}) {
  const requests = [];
  const globals = {
    __env: { VITE_API_BASE: base },
    document: { cookie: csrfCookie ? `detoura_csrf=${csrfCookie}` : '' },
    Headers,
    fetch: async (url, init) => {
      requests.push({ url, init });
      return { ok: status >= 200 && status < 300, status, json: async () => body };
    },
  };
  const mod = moduleFrom(read('api/client.ts'), globals, (name) => {
    if (name === './types') return { DetouraApiError: FakeDetouraApiError };
    return {};
  });
  return { mod, requests };
}

// The Google entry point is always a fixed, same-origin, backend-owned
// path - never built from location.search, a referrer, or any other
// request-controlled input, so there is no way for it to become an open
// redirect (§8/§19 of the consumer UI brief).
{
  const { mod } = loadClient({ base: undefined });
  assert.equal(mod.googleAuthStartUrl(), '/api/v1/auth/google/start');
}
{
  const { mod } = loadClient({ base: 'https://api.detoura.example/api/v1' });
  assert.equal(mod.googleAuthStartUrl(), 'https://api.detoura.example/api/v1/auth/google/start');
}

// googleLinkConfirm: POST, CSRF header attached from the existing
// double-submit cookie (the same mechanism every other mutation uses -
// nothing Google-specific was invented), credentials included, and the
// link id travels only in the JSON body, never a URL/query parameter.
{
  const { mod, requests } = loadClient({ csrfCookie: 'csrf-xyz' });
  const result = await mod.api.googleLinkConfirm({ link_id: 'glink_test123' });
  sameShape(result, { ok: true });
  assert.equal(requests.length, 1);
  const [{ url, init }] = requests;
  assert.equal(url, '/api/v1/auth/google/link/confirm');
  assert.equal(init.method, 'POST');
  assert.equal(init.credentials, 'include');
  assert.equal(init.headers['X-CSRF-Token'], 'csrf-xyz');
  assert.equal(init.body, JSON.stringify({ link_id: 'glink_test123' }));
  assert(!url.includes('glink_test123'), 'link id must never appear in the URL');
}

// A 409 (backend GoogleLinkConflict) and a 400 (generic GoogleAuthError)
// both surface as a normal DetouraApiError - App.tsx maps `status` to a
// safe notice; the raw backend message is never assumed safe to show
// as-is for the conflict case specifically (App.tsx keys off `status`,
// not `message`).
{
  const { mod } = loadClient({ status: 409, body: { detail: { message: 'internal detail' } } });
  await assert.rejects(
    () => mod.api.googleLinkConfirm({ link_id: 'x' }),
    (err) => err instanceof FakeDetouraApiError && err.status === 409,
  );
}

/* ------------------------------------------------------------------ */
/* Static source checks - no browser storage, no raw secrets rendered,  */
/* correct button semantics, App.tsx wiring shape.                     */
/* ------------------------------------------------------------------ */
const loginSource = read('screens/Login.tsx');
const appSource = read('App.tsx');
const clientSource = read('api/client.ts');

assert(loginSource.includes('Continue with Google'), 'button label present');
assert(/type="button"\s*\n\s*className="account-google"/.test(loginSource), 'Google control is a real <button type="button">, not a link/div');
assert(/className="account-google"[\s\S]{0,120}disabled=\{googleRedirecting\}/.test(loginSource), 'disabled while redirecting - prevents double activation');
assert(loginSource.includes('aria-hidden="true"'), 'decorative G mark stays out of the accessibility tree');

// No Google/session/auth value is ever written to localStorage/sessionStorage
// by any of the files this integration touches (§15 of the brief).
for (const [name, source] of [['Login.tsx', loginSource], ['App.tsx', appSource], ['client.ts', clientSource], ['googleAuthReturn.ts', read('lib/googleAuthReturn.ts')]]) {
  assert(!/localStorage\.(setItem|getItem)/.test(source), `${name}: no localStorage use`);
  assert(!/sessionStorage\.(setItem|getItem)/.test(source), `${name}: no sessionStorage use`);
}

// The frontend never constructs a Google authorization URL, never
// references a client secret/id, and never touches an id_token/access_token.
for (const [name, source] of [['Login.tsx', loginSource], ['App.tsx', appSource], ['client.ts', clientSource]]) {
  assert(!/accounts\.google\.com/.test(source), `${name}: no client-built Google URL`);
  assert(!/client_secret|CLIENT_SECRET/.test(source), `${name}: no client secret reference`);
  assert(!/id_token|access_token/i.test(source), `${name}: no token handling`);
}

// App.tsx reads the return outcome exactly once (mount effect), confirms
// the callback via the real api client, and never auto-retries a failed
// link confirmation (no loop/interval near the confirm call).
assert(appSource.includes('readGoogleReturnOutcome(window.location.search)'), 'App.tsx reads the real backend redirect params');
assert(appSource.includes('clearGoogleReturnParams()'), 'App.tsx clears them so a refresh cannot replay the notice');
assert(appSource.includes('api.googleLinkConfirm({ link_id: pendingLinkId })'), 'App.tsx uses the real client method, not a hand-built fetch');
assert(!/setInterval[\s\S]{0,200}googleLinkConfirm/.test(appSource), 'link confirmation is never polled/auto-retried');

console.log('PASS: Google return-outcome parsing, link-id validation, no-secret-leak, googleAuthStartUrl same-origin safety, googleLinkConfirm CSRF/method/body, and static no-storage/no-token/no-open-redirect checks. No network, no Google endpoint, no provider mutation.');

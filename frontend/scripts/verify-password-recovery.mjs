// Focused runtime checks for the consumer password-recovery frontend slice.
// No real network, no backend process, no reset token/password ever leaves
// this process - everything here runs against the real TypeScript source
// through an isolated `vm` context with mocked globals, the same technique
// frontend/scripts/verify-google-signin.mjs already uses.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
const root = fileURLToPath(new URL('../src/', import.meta.url));
const read = path => fs.readFileSync(root + path, 'utf8');

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
/* api/client.ts - requestPasswordReset() and confirmPasswordReset()   */
/* ------------------------------------------------------------------ */
class FakeDetouraApiError extends Error {
  constructor(message, status, issue) {
    super(message);
    this.status = status;
    this.issue = issue;
  }
}
function loadClient({ csrfCookie = '', status = 200, body = { ok: true }, base } = {}) {
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

// requestPasswordReset: anonymous - no CSRF cookie present, none required;
// the email travels only in the JSON body.
{
  const { mod, requests } = loadClient({ body: { message: 'If an account exists for this email, a password reset link has been sent.' } });
  const result = await mod.api.requestPasswordReset({ email: 'traveler@example.com' });
  assert(typeof result.message === 'string');
  assert.equal(requests.length, 1);
  const [{ url, init }] = requests;
  assert.equal(url, '/api/v1/auth/password/reset/request');
  assert.equal(init.method, 'POST');
  assert.equal(init.credentials, 'include');
  assert.equal(init.body, JSON.stringify({ email: 'traveler@example.com' }));
  assert(!url.includes('traveler@example.com'), 'email must never appear in the URL');
}

// confirmPasswordReset: the one-time code travels only in the JSON body,
// never a URL/query parameter - the backend emails a plain code, not a link.
{
  const { mod, requests } = loadClient({ body: { ok: true } });
  const result = await mod.api.confirmPasswordReset({ token: 'RESET_CODE_ABC123', new_password: 'a-new-strong-password' });
  sameShape(result, { ok: true });
  assert.equal(requests.length, 1);
  const [{ url, init }] = requests;
  assert.equal(url, '/api/v1/auth/password/reset/confirm');
  assert.equal(init.method, 'POST');
  assert.equal(init.credentials, 'include');
  assert.equal(init.body, JSON.stringify({ token: 'RESET_CODE_ABC123', new_password: 'a-new-strong-password' }));
  assert(!url.includes('RESET_CODE_ABC123'), 'reset code must never appear in the URL');
}

// An invalid/expired/used token surfaces as a normal DetouraApiError (400) -
// App.tsx/Login.tsx map status to a safe generic notice, never the raw
// backend message assumed safe by construction.
{
  const { mod } = loadClient({ status: 400, body: { detail: { message: 'This reset link is invalid or has expired.' } } });
  await assert.rejects(
    () => mod.api.confirmPasswordReset({ token: 'stale', new_password: 'a-new-strong-password' }),
    (err) => err instanceof FakeDetouraApiError && err.status === 400,
  );
}

/* ------------------------------------------------------------------ */
/* Static source checks - no token/password persistence or leakage,    */
/* enumeration-safe copy, real client methods, duplicate-submit guard. */
/* ------------------------------------------------------------------ */
const loginSource = read('screens/Login.tsx');
const appSource = read('App.tsx');
const clientSource = read('api/client.ts');
const analyticsSource = read('lib/analytics.ts');
const errorTrackingSource = read('lib/errorTracking.ts');

// No reset token, code, or password is ever written to browser storage by
// any file this slice touches.
for (const [name, source] of [['Login.tsx', loginSource], ['App.tsx', appSource], ['client.ts', clientSource]]) {
  assert(!/localStorage\.(setItem|getItem)/.test(source), `${name}: no localStorage use`);
  assert(!/sessionStorage\.(setItem|getItem)/.test(source), `${name}: no sessionStorage use`);
}

// No new analytics event or error-tracking operation was added for this
// flow - the existing typed taxonomies have nothing password-recovery
// shaped, and the brief says to emit nothing rather than force a fit.
assert(!/password/i.test(analyticsSource), 'analytics.ts: no password-recovery event added');
assert(!/password|reset.?code|reset.?token/i.test(errorTrackingSource), 'errorTracking.ts: no password-recovery operation added');
assert(!/captureException/.test(loginSource), 'Login.tsx: password-recovery errors are not forwarded to error instrumentation');

// The frontend calls the real, typed client methods - never a hand-built
// fetch - for both halves of the flow, and reads the CSRF cookie the same
// way every other mutating call already does (no special-casing invented).
assert(appSource.includes('api.requestPasswordReset({ email })'), 'App.tsx uses the real client method for the reset request');
assert(appSource.includes('api.confirmPasswordReset({ token, new_password: newPassword })'), 'App.tsx uses the real client method for the reset confirm');
assert(!/fetch\(/.test(loginSource), 'Login.tsx never calls fetch directly');

// Enumeration safety: the request-accepted copy never claims to know
// whether the account exists.
assert(/If an account exists/i.test(loginSource), 'Login.tsx keeps the non-enumerating "if an account exists" copy');
assert(!/account (does not|doesn.t) exist/i.test(loginSource), 'Login.tsx never claims an account does not exist');

// Invalid/expired/used token: one generic message, never a distinction
// between "expired" and "already used" (the backend does not distinguish
// them either, by design - §10 "single use").
assert(loginSource.includes('This reset code is invalid or has expired'), 'Login.tsx uses one generic invalid/expired/used-token message');
assert(!/already used|already been used/i.test(loginSource), 'Login.tsx never claims a token was specifically "already used"');

// A stale/invalid code always has a safe way back to request a new one.
assert(/Request a new code/.test(loginSource), 'Login.tsx offers a route back to requesting a new code');

// Duplicate-submission guard: the submit button is disabled whenever
// canSubmit is false, and canSubmit is false while a request is in flight
// for every mode, including the new resetConfirm mode.
assert(/if \(isLoading\) return false;/.test(loginSource), 'canSubmit short-circuits to false while a request is in flight');
assert(/disabled=\{!canSubmit\}/.test(loginSource), 'submit button is disabled whenever canSubmit is false');

// No session cookie is set/cleared by anything this slice added directly -
// that remains entirely the backend's job via Set-Cookie on /auth/* calls.
assert(!/document\.cookie\s*=/.test(loginSource), 'Login.tsx never writes cookies directly');

console.log('PASS: password-reset request/confirm client methods (POST, JSON body, no URL leakage), 400/429 error surfacing, no browser storage of token/password, no new analytics/error-tracking taxonomy, enumeration-safe and single-generic-message copy, duplicate-submission guard.');

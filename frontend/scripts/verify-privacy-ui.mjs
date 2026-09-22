// Dependency-free source-bound tests. Node 22.18+; fake clock/storage/network only.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { spawnSync } from 'node:child_process';
import { legalConfig, legalReadinessIssues, requiredLegalFields } from '../src/privacy/legalConfig.ts';
import { noticeSections } from '../src/privacy/noticeContent.ts';
const root = new URL('../src/', import.meta.url);
const read = path => fs.readFileSync(new URL(path, root), 'utf8');
function load(path, globals, names) {
  const source = stripTypeScriptTypes(read(path)).replace(/^import .*?;\n/gms, '').replaceAll('export ', '').replaceAll('import.meta.env', '__env');
  return vm.runInNewContext(`${source}\n;({${names.join(',')}})`, globals);
}
const key = 'detoura-journey-draft-v1';
let now = Date.UTC(2026, 8, 22), writes = 0;
const storage = new Map();
class Clock extends Date { static now() { return now; } }
const localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => { ++writes; storage.set(key, value); },
  removeItem: key => storage.delete(key),
};
const draftApi = load('state/journeyDraft.ts', { Date: Clock, localStorage }, ['makeJourneyDraft', 'saveJourneyDraft', 'loadJourneyDraft', 'JOURNEY_DRAFT_TTL_MS']);
const trip = { id: 'planning-only', cities: ['Berlin'], total_price: 100, currency: 'EUR' };
const draft = draftApi.makeJourneyDraft(trip, { travelers: 2 });
const ttl = 30 * 24 * 60 * 60 * 1000;
assert.equal(draft.expiresAt - draft.createdAt, ttl);
assert.equal(draftApi.JOURNEY_DRAFT_TTL_MS, ttl);
draftApi.saveJourneyDraft(draft);
const stored = storage.get(key), initialWrites = writes;
now += 10 * 24 * 60 * 60 * 1000;
for (let i = 0; i < 10; i++) assert.equal(draftApi.loadJourneyDraft().expiresAt, draft.expiresAt);
assert.equal(writes, initialWrites, 'restore never writes/slides the clock');
assert.equal(storage.get(key), stored);
now = draft.expiresAt - 1;
assert(draftApi.loadJourneyDraft());
now++;
assert.equal(draftApi.loadJourneyDraft(), null, 'expires at exact 30-day boundary');
assert(!storage.has(key));
draftApi.saveJourneyDraft(draft);
assert(!storage.has(key), 'expired memory cannot repersist');
now = draft.createdAt;
for (const patch of [
  { createdAt: undefined, expiresAt: undefined }, // legacy, regardless of selectedAt
  { createdAt: undefined }, { expiresAt: undefined }, { createdAt: 'today' },
  { expiresAt: null }, { expiresAt: draft.expiresAt + 1 },
  { createdAt: now + 1 }, { createdAt: -1 }, { expiresAt: now },
  { selectedAt: 'invalid' }, { selectedAt: new Date(now - ttl * 2).toISOString() },
  { version: 2 }, { createdAt: 1.5 },
]) {
  storage.set(key, JSON.stringify({ ...draft, ...patch }));
  assert.equal(draftApi.loadJourneyDraft(), null, `rejects ${JSON.stringify(patch)}`);
  assert(!storage.has(key));
}
for (const corrupt of ['{', 'null', '[]', '42']) {
  storage.set(key, corrupt); assert.equal(draftApi.loadJourneyDraft(), null); assert(!storage.has(key));
}
const denied = load('state/journeyDraft.ts', { Date: Clock, localStorage: {
  getItem() { throw Error(); }, setItem() { throw Error(); }, removeItem() { throw Error(); },
} }, ['saveJourneyDraft', 'loadJourneyDraft']);
assert.equal(denied.loadJourneyDraft(), null);
assert.doesNotThrow(() => denied.saveJourneyDraft(draft));
now++;
const modified = draftApi.makeJourneyDraft({ ...trip, id: 'new-selection' }, null);
assert.equal(modified.createdAt, now, 'explicit selection can create a new generation');

assert.equal(legalConfig.COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED, false);
assert(legalReadinessIssues().some(issue => issue.startsWith('COMPANION_TRAVELLER')));
for (const field of requiredLegalFields) {
  for (const value of ['', '  ', '[LEGAL ENTITY NAME]', 'TODO', 'example', '<ADDRESS>']) {
    assert(legalReadinessIssues({ ...legalConfig, [field]: value }).some(issue => issue.startsWith(field)));
  }
}
for (const value of ['2026-02-30', '2026-13-22', 'soon']) assert(legalReadinessIssues({ ...legalConfig, PRIVACY_NOTICE_EFFECTIVE_DATE: value }).some(issue => issue.startsWith('PRIVACY_NOTICE_EFFECTIVE_DATE')));
for (const value of ['javascript:alert(1)', 'http://authority.invalid', 'https://user:pass@authority.invalid']) assert(legalReadinessIssues({ ...legalConfig, COMPETENT_SUPERVISORY_AUTHORITY_URL: value }).some(issue => issue.startsWith('COMPETENT_SUPERVISORY_AUTHORITY_URL')));
const release = spawnSync(process.execPath, [new URL('./verify-legal-readiness.mjs', import.meta.url).pathname], { encoding: 'utf8' });
assert.equal(release.status, 1, 'actual production gate fails for current configuration');
assert(release.stderr.includes('COMPANION_TRAVELLER'));
assert.equal(noticeSections.length, 20);
assert.equal(new Set(noticeSections.map(s => s.id)).size, 20);
for (const section of noticeSections) {
  assert(section.en.title && section.de.title);
  assert.equal(section.en.paragraphs.length, section.de.paragraphs.length);
  assert(section.en.paragraphs.every(Boolean) && section.de.paragraphs.every(Boolean));
}
assert(read('main.tsx').includes('isPrivacy ? <Privacy /> : <App />'), 'public route outside account hook');
assert(read('screens/Privacy.tsx').includes('const showNotice = ready || import.meta.env.DEV'));
assert(!/api\.|useAccount|initAnalytics/.test(read('screens/Privacy.tsx')));
assert(read('lib/seo.ts').includes('setCanonical(absolute("/privacy"))'));
assert(read('screens/Landing.tsx').includes('href="/privacy"'));
assert(read('screens/Login.tsx').includes('href="/privacy"'));
const controls = read('components/account/AccountPrivacy.tsx');
assert(controls.includes('Delete your account?'));
assert(controls.includes('Some booking, payment and financial records may need to be kept'));
assert(controls.includes('Export account data'));
assert(controls.includes('For a broader privacy request'));
assert(controls.includes('PRIVACY_CONTACT_EMAIL'));
assert(controls.includes('controller.signal.aborted'));
assert(!/localStorage|sessionStorage/.test(controls));
const app = read('App.tsx');
const runSearch = app.slice(app.indexOf('  const runSearch'), app.indexOf('  const changeProfile'));
assert(!/setJourneyDraft|saveJourneyDraft/.test(runSearch), 'new search preserves valid selection');
assert(app.includes('Math.min(remaining, 2_147_483_647)'), 'long-lived tab expiry handles browser timer limit');
assert(!/saveJourneyDraft/.test(app.slice(app.indexOf('const checkExpiry'), app.indexOf('const journeyModel'))));
for (const path of ['screens/Login.tsx', 'screens/BookingExperience.tsx', 'screens/MyTrips.tsx', 'state/useAccount.ts', 'api/client.ts']) {
  assert(!/(localStorage|sessionStorage)\.(setItem|getItem)/.test(read(path)), `${path}: no sensitive durable storage`);
}
for (const path of ['main.tsx', 'App.tsx', 'screens/Privacy.tsx', 'screens/Login.tsx', 'components/account/AccountPrivacy.tsx']) {
  assert(!/setAnalyticsConsent\s*\(|analytics:\s*true|marketing:\s*true|Accept All|Reject All|CookieBanner|ConsentCenter/.test(read(path)), `${path}: no tracking activation/CMP`);
}
assert(!/health condition|disability details|religious dietary|consent on behalf/i.test(read('screens/BookingExperience.tsx')));
assert(!/Delete all my data|Everything will be permanently deleted|All records will be erased|Download all my data|Complete GDPR export|Full privacy export/i.test(controls));
// Exercise actual client protocol: cookie credentials, CSRF, body, partial export,
// ownership-only document routes and refusal to flatten server failures.
const calls = [];
let status = 200;
class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
const client = load('api/client.ts', {
  __env: {}, Headers, DetouraApiError: ApiError, document: { cookie: 'detoura_csrf=test-csrf' },
  fetch: async (url, init) => { calls.push({ url, init }); return { ok: status === 200, status, json: async () => status === 200 ? { ok: true } : { detail: 'mock rejection' }, blob: async () => new Blob(['fixture']) }; },
}, ['api']).api;
await client.deleteAccount('memory-only-password');
assert.equal(calls.at(-1).url, '/api/v1/auth/account/delete');
assert.equal(calls.at(-1).init.method, 'POST');
assert.equal(calls.at(-1).init.headers['X-CSRF-Token'], 'test-csrf');
assert.equal(calls.at(-1).init.credentials, 'include');
assert.deepEqual(JSON.parse(calls.at(-1).init.body), { current_password: 'memory-only-password' });
await client.deleteAccount();
assert.deepEqual(JSON.parse(calls.at(-1).init.body), {});
await client.exportAccountData();
assert.equal(calls.at(-1).url, '/api/v1/auth/account/export');
assert.equal(calls.at(-1).init.cache, 'no-store');
assert.equal(calls.at(-1).init.credentials, 'include');
await client.downloadTripDocument('booking/id', 'doc/id');
assert.equal(calls.at(-1).url, '/api/v1/me/trips/booking%2Fid/documents/doc%2Fid/download');
status = 401;
await assert.rejects(() => client.exportAccountData());
await assert.rejects(() => client.deleteAccount());
await assert.rejects(() => client.downloadTripDocument('other', 'other'));
console.log('PASS: privacy route/content/config gates, exact TTL/migration/non-sliding restore, storage denial, account protocol and document failure checks. Mock runtime, no external side effects.');

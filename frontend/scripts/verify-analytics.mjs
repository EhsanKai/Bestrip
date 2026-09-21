// Focused runtime checks using the installed TypeScript compiler and isolated browser mocks.
// No real network, provider objects, account state, or browser storage is accessed.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
const root = fileURLToPath(new URL('../src/', import.meta.url));
const read = path => fs.readFileSync(root + path, 'utf8');
function moduleFrom(source, globals, require = () => ({})) {
  const exports = {};
  const js = ts.transpileModule(source.replaceAll('import.meta.env', '__env'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  vm.runInNewContext(js, { exports, require, ...globals });
  return exports;
}
function harness({ dev = false, denyStorage = false, beaconThrows = false } = {}) {
  const session = new Map(), local = new Map(), timers = new Map(), listeners = new Map();
  const logs = [], sends = [], fetches = [];
  let timer = 0;
  const storage = map => ({
    getItem: key => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
    removeItem: key => map.delete(key),
  });
  const window = {
    innerWidth: 1000,
    location: { search: '?utm_source=Beta&utm_medium=email&utm_campaign=v9&utm_term=javascript%3Aalert(1)&email=private', pathname: '/', origin: 'https://detoura.example' },
    setTimeout: fn => { timers.set(++timer, fn); return timer; },
    clearTimeout: id => timers.delete(id),
    addEventListener: (key, fn) => listeners.set(key, fn),
  };
  for (const [key, value] of [['sessionStorage', storage(session)], ['localStorage', storage(local)]]) {
    Object.defineProperty(window, key, { get() { if (denyStorage) throw Error('denied'); return value; } });
  }
  const globals = {
    window,
    document: { referrer: 'https://example.org/private?token=secret', visibilityState: 'hidden', addEventListener: (key, fn) => listeners.set(key, fn) },
    __env: { DEV: dev, VITE_ANALYTICS_FIRST_PARTY: 'true', VITE_ANALYTICS_PROVIDER: 'ga4' },
    URL, URLSearchParams, Blob,
    crypto: { getRandomValues: values => values.fill(13) },
    navigator: { sendBeacon: (url, body) => { if (beaconThrows) throw Error('denied'); sends.push({ url, body }); return true; } },
    fetch: (...args) => { fetches.push(args); return Promise.resolve({}); },
    console: { debug: (...args) => logs.push(args), error: (...args) => logs.push(args), warn() {} },
  };
  for (const key of ['sessionStorage', 'localStorage']) Object.defineProperty(globals, key, { enumerable: true, get: () => {
    // Module globals use throwing methods for storage-denial tests as well.
    if (denyStorage) return { getItem() { throw Error(); }, setItem() { throw Error(); }, removeItem() { throw Error(); } };
    return window[key];
  } });
  const schemas = moduleFrom(read('lib/analyticsPayloads.ts'), globals);
  const analytics = moduleFrom(read('lib/analytics.ts'), globals, () => schemas);
  const errorTracking = moduleFrom(read('lib/errorTracking.ts'), globals, () => analytics);
  const flush = () => { const tasks = [...timers.values()]; timers.clear(); tasks.forEach(fn => fn()); };
  return { analytics, errorTracking, session, local, logs, sends, fetches, flush, listeners, window };
}
const h = harness();
h.analytics.init();
h.analytics.track('search_started', { search_mode: 'SMART' }, { dedupeKey: 'search' });
assert.equal(h.session.size + h.local.size + h.sends.length + h.logs.length, 0, 'production default stays silent');
h.analytics.setAnalyticsConsent({ marketing: true });
h.analytics.track('search_started', {});
assert.equal(h.session.size, 0, 'marketing alone does not enable analytics');
h.analytics.setAnalyticsConsent({ analytics: true });
const attribution = JSON.parse(h.session.get('detoura.attribution.v1'));
assert.equal(attribution.utm_source, 'beta');
assert.equal(attribution.referrer_origin, 'external');
assert.equal(attribution.utm_term, undefined);
assert(!JSON.stringify(attribution).includes('secret'));
h.analytics.track('search_started', { search_mode: 'SMART', email: 'sensitive', request: { secret: true } }, { dedupeKey: 'search' });
assert(h.local.has('detoura.fk.v'));
h.flush();
assert.equal(h.sends.length, 1, 'pre-permission event did not consume dedupe key');
assert(!await h.sends[0].body.text().then(text => /sensitive|request|secret/.test(text)));
h.analytics.track('search_completed', { result_count: 2 });
h.analytics.setAnalyticsConsent({ analytics: false });
h.flush();
h.listeners.get('pagehide')();
h.listeners.get('visibilitychange')();
assert.equal(h.sends.length, 1, 'revocation drops queue and blocks later delivery');
assert.equal(h.session.size + h.local.size, 0, 'revocation clears optional storage');
h.analytics.setAnalyticsConsent({ analytics: 'yes' });
h.analytics.track('search_started', {}); h.flush();
assert.equal(h.sends.length, 1, 'runtime grant must be literal true');
const d = harness({ dev: true }); d.analytics.init();
d.analytics.track('booking_confirmed', { booking_state: 'confirmed', tier: 'ALL_IN_ONE', email: 'secret', payment_id: 'secret' }, { dedupeKey: 'confirmed' });
d.analytics.track('booking_confirmed', { booking_state: 'confirmed' }, { dedupeKey: 'confirmed' });
d.analytics.track('booking_confirmed', { booking_state: 'self_service_ready' });
d.analytics.track('unknown', { token: 'secret' });
assert.equal(d.logs.length, 1);
assert.deepEqual(Object.keys(d.logs[0][2]).sort(), ['booking_state', 'tier', 'viewport_class']);
d.errorTracking.captureException(new Error('secret provider payload'), { operation: 'search', request: { secret: true } });
d.errorTracking.reportIssue({ summary: 'secret support text', context: { request: 'secret' } });
assert.equal(d.logs.length, 2);
assert(!JSON.stringify(d.logs).includes('secret'));
for (const bad of [null, { get search_mode() { throw Error('bad getter'); } }]) assert.doesNotThrow(() => d.analytics.track('search_started', bad));
const unavailable = harness({ denyStorage: true, beaconThrows: true });
unavailable.analytics.init(); unavailable.analytics.setAnalyticsConsent({ analytics: true });
assert.doesNotThrow(() => unavailable.analytics.track('search_started', {}));
assert.doesNotThrow(() => unavailable.flush());
assert.equal(unavailable.fetches.length, 1, 'beacon/storage failure falls back without breaking product');
// Stored attribution is untrusted. Invalid expiry and oversized records must be discarded.
for (const raw of ['{"captured_at":"invalid"}', 'x'.repeat(3000), JSON.stringify({ captured_at: new Date(Date.now() + 100000).toISOString() })]) {
  d.analytics.setAnalyticsConsent({ analytics: true }); d.session.set('detoura.attribution.v1', raw);
  d.analytics.track('search_started', {});
  assert(!d.session.has('detoura.attribution.v1'));
}
for (const query of ['?utm_source=https://private.example/secret', '?utm_source=' + 'a'.repeat(81), '?utm_source=a%40b.com', '?utm_source=%3Cscript%3E', '?utm_source=%252Fsecret']) {
  const a = harness(); a.window.location.search = query; a.window.location.pathname = '/booking/private-id';
  a.analytics.init(); a.analytics.setAnalyticsConsent({ analytics: true });
  const record = JSON.parse(a.session.get('detoura.attribution.v1'));
  assert.equal(record.utm_source, undefined); assert.equal(record.landing_path, undefined);
}
// Execute actual checkout handlers with mocked React hooks and backend responses.
// Only replace the render return; all handler logic is the production source.
const bookingSource = read('screens/BookingExperience.tsx').replace('  const labels = stepLabels(flow);', '  return { confirm, authorizePayment };\n  const labels = stepLabels(flow);');
async function checkout({ status = 'AUTHORIZED', throwPayment = false, throwBooking = false, flow = 'managed', passStatus = 'ready' } = {}) {
  const events = [], effects = [], timers = [];
  let index = 0;
  const initial = { 0: 'working', 3: 'test-booking', 4: { currency: 'EUR' }, 5: { status: 'AUTHORIZED' }, 10: 'ALL_IN_ONE' };
  const react = {
    useState: value => [Object.hasOwn(initial, index) ? initial[index++] : (index++, typeof value === 'function' ? value() : value), () => {}],
    useRef: value => ({ current: value }), useCallback: fn => fn, useEffect: fn => effects.push(fn),
  };
  const api = {
    createPayment: async () => { if (throwPayment) throw Error('private'); return { payment_id: 'private-payment' }; },
    confirmPayment: async () => ({ status, currency: 'EUR', payment_id: 'private-payment' }),
    confirmBooking: async () => { if (throwBooking) throw Error('private'); return { service_flow: flow }; },
    getItinerary: async () => ({}),
    getBookingIntent: async () => ({ pass_available: true }),
    getTravelPass: async () => ({ status: passStatus }),
  };
  class DetouraApiError extends Error {}
  const { BookingExperience } = moduleFrom(bookingSource, { window: { setTimeout: fn => timers.push(fn), clearTimeout() {} }, crypto: { randomUUID: () => 'test' } }, name => {
    if (name === 'react') return react;
    if (name.endsWith('/client')) return { api };
    if (name.endsWith('/types')) return { DetouraApiError };
    if (name.endsWith('/analytics')) return { track: (event, props) => events.push({ event, props }), classifyAnalyticsError: () => 'network' };
    return {};
  });
  const handlers = BookingExperience({ trip: { legs: [], rank: 1, currency: 'EUR', selection_id: 'private' }, onBack() {}, onViewDetails() {} });
  await handlers.authorizePayment(); await handlers.confirm();
  effects[2](); await timers[0]();
  return events;
}
let events = await checkout({ throwPayment: true, throwBooking: true, passStatus: 'unrecognized' });
assert(events.some(e => e.event === 'payment_request_failed'));
assert(events.some(e => e.event === 'booking_confirmation_failed'));
assert(!events.some(e => ['payment_failed', 'booking_failed', 'booking_confirmed'].includes(e.event)));
for (const status of ['UNKNOWN', 'RECONCILIATION_REQUIRED', 'FAILED', 'CANCELLED', 'AUTHORIZED']) {
  events = await checkout({ status, passStatus: 'failed' });
  const name = status === 'AUTHORIZED' ? 'payment_authorized' : ['FAILED', 'CANCELLED'].includes(status) ? 'payment_failed' : 'payment_unknown';
  assert(events.some(e => e.event === name && e.props.payment_state === status));
  assert(events.some(e => e.event === 'booking_failed'));
  assert(!JSON.stringify(events).includes('private'));
}
events = await checkout({ flow: 'self_service', passStatus: 'recovery_required' });
assert(events.some(e => e.event === 'self_service_ready'));
assert(events.some(e => e.event === 'booking_recovery_required'));
assert(!events.some(e => e.event === 'booking_confirmed' || e.event === 'booking_failed'));
events = await checkout();
assert(events.some(e => e.event === 'booking_confirmed'));
assert(!/captureException\([^;]*\{\s*(request|trip_id)/.test(read('state/useSearch.ts') + read('state/useSaved.ts')));
console.log('PASS: permission, revocation, storage, runtime allowlists, failure isolation, error privacy, and actual checkout handler outcome checks. No provider/network mutations.');

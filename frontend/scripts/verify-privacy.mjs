// Privacy regression checks against real TS/TSX in isolated mocked runtimes.
// No browser/user storage, real HTTP requests, email, or provider mutations.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../src/', import.meta.url));
const read = path => fs.readFileSync(root + path, 'utf8');
function load(path, globals = {}, require = () => ({})) {
  const exports = {};
  const code = ts.transpileModule(read(path).replaceAll('import.meta.env', '__env'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  vm.runInNewContext(code, { exports, require, ...globals });
  return exports;
}
function hooks() {
  let cursor = 0;
  const cells = [], effects = [];
  return {
    cells, effects,
    render(fn) { cursor = 0; return fn(); },
    react: {
      useState(initial) {
        const index = cursor++;
        if (!(index in cells)) cells[index] = typeof initial === 'function' ? initial() : initial;
        return [cells[index], value => { cells[index] = typeof value === 'function' ? value(cells[index]) : value; }];
      },
      useRef(initial) { const index = cursor++; return cells[index] ??= { current: initial }; },
      useEffect(fn) { effects.push(fn); },
      useCallback: fn => fn,
      useMemo: fn => fn(),
    },
  };
}
class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
const tick = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
// An older restoration cannot replace a newer successful login/logout.
{
  const host = hooks(); let release; let requests = 0;
  const api = { me: () => ++requests === 1 ? new Promise(resolve => { release = resolve; }) : Promise.resolve({ user_id: 'new-account' }), login: async () => {}, logout: async () => {} };
  const mod = load('state/useAccount.ts', {}, name => name === 'react' ? host.react : name.endsWith('/client') ? { api } : { DetouraApiError: ApiError });
  const account = host.render(() => mod.useAccount());
  const pending = account.refresh(); await account.login('test@example.invalid', 'mock-only');
  release(null); await pending;
  assert.equal(host.cells[0].profile.user_id, 'new-account');
  api.me = () => new Promise(resolve => { release = resolve; });
  const stale = account.refresh(); await account.logout(); release({ user_id: 'old-account' }); await stale;
  assert.equal(host.cells[0].status, 'anonymous'); assert.equal(host.cells[0].profile, null);
}
// Failed login must settle state after invalidating an initial restoration.
{
  const host = hooks(); let release;
  const api = { me: () => new Promise(resolve => { release = resolve; }), login: async () => { throw new ApiError('denied', 401); } };
  const mod = load('state/useAccount.ts', {}, name => name === 'react' ? host.react : name.endsWith('/client') ? { api } : { DetouraApiError: ApiError });
  const account = host.render(() => mod.useAccount()); const pending = account.refresh();
  await assert.rejects(() => account.login('test@example.invalid', 'mock-only'));
  release(null); await pending; assert.equal(host.cells[0].status, 'error');
}
// Revalidation during a mutation must run after it, not be silently lost.
{
  const host = hooks(); let release; let calls = 0;
  const api = { me: async () => ++calls === 1 ? { user_id: 'stale' } : null, login: () => new Promise(resolve => { release = resolve; }) };
  const mod = load('state/useAccount.ts', {}, name => name === 'react' ? host.react : name.endsWith('/client') ? { api } : { DetouraApiError: ApiError });
  const account = host.render(() => mod.useAccount()); const login = account.login('test@example.invalid', 'mock-only');
  await account.refresh(); release(); await login;
  assert.equal(calls, 2); assert.equal(host.cells[0].status, 'anonymous');
}
// Session changes in another tab and focus/pageshow revalidate authoritative /me.
{
  const host = hooks(), listeners = new Map(), channels = [];
  let profile = { user_id: 'account-a' };
  const api = { me: async () => profile };
  class Channel { constructor(name) { this.name = name; this.messages = []; channels.push(this); } postMessage(value) { this.messages.push(value); } close() { this.closed = true; } }
  const mod = load('state/useAccount.ts', { BroadcastChannel: Channel, AbortController, window: { addEventListener: (key, fn) => listeners.set(key, fn), removeEventListener: key => listeners.delete(key) } }, name => name === 'react' ? host.react : name.endsWith('/client') ? { api } : { DetouraApiError: ApiError });
  host.render(() => mod.useAccount()); const cleanup = host.effects[0](); await tick();
  assert.equal(host.cells[0].profile.user_id, 'account-a');
  profile = { user_id: 'account-b' }; channels[0].onmessage({ data: 'session-changed' }); await tick();
  assert.equal(host.cells[0].profile.user_id, 'account-b');
  api.me = async () => { throw new ApiError('offline', 0); }; listeners.get('focus')(); await tick();
  assert.equal(host.cells[0].profile.user_id, 'account-b'); assert.equal(host.cells[0].status, 'error');
  api.me = async () => profile;
  profile = null; listeners.get('focus')(); await tick();
  assert.equal(host.cells[0].status, 'anonymous');
  assert(channels[0].messages.every(value => value === 'session-changed'), 'channel never transmits credentials or identity');
  cleanup(); assert.equal(listeners.size, 0); assert(channels[0].closed);
}
// Ops token migration discards old storage, keeps credentials only in memory,
// authenticates existing API calls, and clears on 401.
{
  const store = new Map([['detoura.ops.session', 'legacy-secret']]); let request; let status = 200;
  const mod = load('ops/opsApi.ts', {
    __env: {}, sessionStorage: { removeItem: key => store.delete(key), setItem() { throw Error('must not persist token'); } },
    fetch: async (url, init) => { request = { url, init }; return { ok: status === 200, status, json: async () => ({}) }; },
  });
  assert.equal(store.size, 0); assert.equal(mod.getToken(), '');
  mod.setToken('memory-secret'); await mod.opsApi.overview();
  assert.equal(request.init.headers.Authorization, 'Bearer memory-secret'); assert.equal(store.size, 0);
  status = 401; await assert.rejects(() => mod.opsApi.overview()); assert.equal(mod.getToken(), '');
  const denied = load('ops/opsApi.ts', { __env: {}, sessionStorage: { removeItem() { throw Error('denied'); } } });
  denied.setToken('memory-only'); assert.equal(denied.getToken(), 'memory-only');
  assert(read('main.tsx').includes('sessionStorage.removeItem("detoura.ops.session")'));
}
const jsx = (type, props) => ({ type, props });
function walk(node, predicate) {
  if (!node || typeof node !== 'object') return undefined;
  if (predicate(node)) return node;
  const children = Array.isArray(node) ? node : Object.values(node.props ?? {});
  for (const child of children) { const found = walk(child, predicate); if (found) return found; }
}
function loginHarness(props) {
  const host = hooks();
  const { Login } = load('screens/Login.tsx', {}, name => name === 'react' ? host.react : name === 'react/jsx-runtime' ? { jsx, jsxs: jsx } : name.endsWith('/types') ? { DetouraApiError: ApiError } : {});
  return { host, render: () => host.render(() => Login(props)) };
}
// A completed reset discards both passwords and its one-time code immediately.
{
  let submitted;
  const h = loginHarness({ initialMode: 'resetConfirm', onConfirmResetPassword: async payload => { submitted = payload; } });
  let tree = h.render();
  for (const [id, value] of [['account-reset-code', 'test-code'], ['account-password', 'test-password'], ['account-confirm-password', 'test-password']]) {
    walk(tree, node => node.props?.id === id).props.onChange(value);
  }
  tree = h.render(); await walk(tree, node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  assert.equal(submitted.token, 'test-code');
  assert.equal(h.host.cells[0], 'passwordUpdated');
  assert(!JSON.stringify(h.host.cells).includes('test-code')); assert(!JSON.stringify(h.host.cells).includes('test-password'));
}
// Successful login clears credentials; failed logout and failed Google linking
// remain visible in the authenticated view instead of silently disappearing.
{
  const h = loginHarness({ onLogin: async () => {} }); let tree = h.render();
  walk(tree, node => node.props?.id === 'account-email').props.onChange('test@example.invalid');
  walk(tree, node => node.props?.id === 'account-password').props.onChange('test-password');
  tree = h.render(); await walk(tree, node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  assert(!JSON.stringify(h.host.cells).includes('test-password'));
  const authenticated = loginHarness({ profile: { email_normalized: 'test@example.invalid' }, sessionStatus: 'authenticated', googleNotice: { kind: 'error', reason: 'link_conflict' }, onLogout: async () => { throw new ApiError('sensitive backend detail', 0); } });
  tree = authenticated.render(); assert(JSON.stringify(tree).includes('already connected to a different'));
  await walk(tree, node => node.type === 'button' && node.props.onClick?.name === 'handleLogout').props.onClick();
  tree = authenticated.render();
  assert(walk(tree, node => node.props?.role === 'alert'));
  assert(!JSON.stringify(tree).includes('sensitive backend detail'));
}
// Execute the actual Google-link callback: a late result after logout/account
// departure cannot restore another account's success/error hint.
{
  const source = read('App.tsx');
  const start = source.indexOf('    const attempt = ++googleLinkAttempt.current;');
  const end = source.indexOf('  }, [account, googleLinkId]);', start);
  const body = source.slice(start, end);
  for (const rejectResult of [false, true]) {
    let resolve, reject; const hints = []; const generation = { current: 0 };
    const callback = ts.transpileModule(`globalThis.run = async (payload: {email:string;password:string}) => {${body}}`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const context = vm.createContext({
      account: { login: async () => {} }, googleLinkId: 'mock-ticket', googleLinkAttempt: generation,
      api: { googleLinkConfirm: () => new Promise((ok, fail) => { resolve = ok; reject = fail; }) },
      setGoogleLinkId: () => {}, setGoogleNotice: value => hints.push(value), setGoogleLinked: value => hints.push(value),
      DetouraApiError: ApiError,
    });
    vm.runInContext(callback, context);
    const pending = context.run({email:'test@example.invalid',password:'mock-only'}); await tick();
    ++generation.current;
    if (rejectResult) reject(new ApiError('mock failure', 409)); else resolve({});
    await pending; assert.deepEqual(hints, []);
  }
}
assert(read('App.tsx').includes('key={`${account.profile?.user_id ?? "anonymous"}:${selected.id}`}'));
assert(read('App.tsx').includes('key={account.profile?.user_id ?? "anonymous"}'));
assert(read('screens/MyTrips.tsx').includes('if (signal?.aborted) return;'));
console.log('PASS: stale-session races, account-change revalidation, secret-free tab notifications, Ops token migration/memory/401 handling, credential cleanup and authenticated error visibility. No external side effects.');

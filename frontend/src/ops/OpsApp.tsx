import { useCallback, useEffect, useState } from "react";
import {
  getToken,
  opsApi,
  OpsError,
  setToken,
  type OpsOverview,
} from "./opsApi";
import { BookingsView } from "./OpsBookings";
import { RecoveryView } from "./OpsRecovery";
import { AuditView } from "./OpsAudit";
import { CommercialView } from "./OpsCommercial";
import { PromosView } from "./OpsPromos";
import { FinanceView } from "./OpsFinance";
import { AnalyticsView } from "./OpsAnalytics";
import "./ops.css";

type View =
  | "bookings"
  | "recovery"
  | "commercial"
  | "promos"
  | "finance"
  | "analytics"
  | "audit";

const NAV: { id: View; label: string }[] = [
  { id: "bookings", label: "Bookings" },
  { id: "recovery", label: "Recovery" },
  { id: "commercial", label: "Commercial" },
  { id: "promos", label: "Promos" },
  { id: "finance", label: "Finance" },
  { id: "analytics", label: "Analytics" },
  { id: "audit", label: "Audit log" },
];

export function OpsApp() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [authed, setAuthed] = useState<boolean>(() => Boolean(getToken()));
  const [view, setView] = useState<View>("bookings");
  const [overview, setOverview] = useState<OpsOverview | null>(null);

  useEffect(() => {
    opsApi
      .status()
      .then((s) => setEnabled(s.enabled))
      .catch(() => setEnabled(false));
  }, []);

  const refreshOverview = useCallback(() => {
    if (!authed) return;
    opsApi
      .overview()
      .then(setOverview)
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) {
          setToken("");
          setAuthed(false);
        }
      });
  }, [authed]);

  useEffect(() => {
    refreshOverview();
    const t = window.setInterval(refreshOverview, 15000);
    return () => window.clearInterval(t);
  }, [refreshOverview]);

  const signOut = async () => {
    try {
      await opsApi.logout();
    } catch {
      /* ignore */
    }
    setToken("");
    setAuthed(false);
    setOverview(null);
  };

  if (enabled === null) {
    return <div className="ops ops--center">Loading…</div>;
  }
  if (!enabled) {
    return (
      <div className="ops ops--center">
        <div className="ops-card">
          <h1>Detoura Ops</h1>
          <p className="ops-muted">
            The ops console is not configured on this deployment.
            Set <code>DETOURA_OPS_TOKEN</code> and restart.
          </p>
        </div>
      </div>
    );
  }
  if (!authed) {
    return <OpsLogin onAuthed={() => setAuthed(true)} />;
  }

  return (
    <div className="ops">
      <header className="ops-top">
        <div className="ops-top__brand">
          <b>DETOURA OPS</b>
          <span className="ops-pill ops-pill--test">TEST / SANDBOX</span>
        </div>
        <nav className="ops-top__nav">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "is-on" : ""}
              onClick={() => setView(n.id)}
            >
              {n.label}
              {n.id === "bookings" && overview ? (
                <span className="ops-badge">{overview.total_bookings}</span>
              ) : null}
              {n.id === "recovery" && overview && overview.recovery_count > 0 ? (
                <span className="ops-badge ops-badge--warn">
                  {overview.recovery_count}
                </span>
              ) : null}
            </button>
          ))}
        </nav>
        <button className="ops-top__out" onClick={signOut}>
          Sign out
        </button>
      </header>

      <main className="ops-main">
        {view === "bookings" && <BookingsView onExpire={() => setAuthed(false)} />}
        {view === "recovery" && <RecoveryView onExpire={() => setAuthed(false)} />}
        {view === "commercial" && <CommercialView onExpire={() => setAuthed(false)} />}
        {view === "promos" && <PromosView onExpire={() => setAuthed(false)} />}
        {view === "finance" && <FinanceView onExpire={() => setAuthed(false)} />}
        {view === "analytics" && <AnalyticsView onExpire={() => setAuthed(false)} />}
        {view === "audit" && <AuditView onExpire={() => setAuthed(false)} />}
      </main>
    </div>
  );
}

function OpsLogin({ onAuthed }: { onAuthed: () => void }) {
  const [token, setTokenInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await opsApi.login(token.trim());
      setToken(r.session_token);
      onAuthed();
    } catch (err) {
      setError(err instanceof OpsError ? err.message : "Sign in failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ops ops--center">
      <form className="ops-card" onSubmit={submit}>
        <h1>Detoura Ops</h1>
        <span className="ops-pill ops-pill--test">TEST / SANDBOX</span>
        <label className="ops-field">
          Ops token
          <input
            type="password"
            value={token}
            autoFocus
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="DETOURA_OPS_TOKEN"
          />
        </label>
        {error && <p className="ops-error">{error}</p>}
        <button className="ops-btn" disabled={busy || !token.trim()}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

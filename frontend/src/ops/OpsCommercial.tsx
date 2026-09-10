import { useCallback, useEffect, useState } from "react";
import {
  opsApi,
  OpsError,
  type MarkupPolicy,
  type MarkupPolicyConfig,
  type MarkupPreview,
} from "./opsApi";
import { money, when } from "./opsFormat";

const BLANK: MarkupPolicyConfig = {
  basic_percentage: 0.03,
  basic_fixed_fee: 0,
  all_in_one_percentage: 0.05,
  all_in_one_fixed_fee: 6,
  max_percentage: 0.15,
  max_fixed_fee: 25,
  min_total_fee: 0,
  max_total_fee: 120,
};

const pct = (n: number) => `${(n * 100).toFixed(2)}%`;

export function CommercialView({ onExpire }: { onExpire: () => void }) {
  const [rows, setRows] = useState<MarkupPolicy[]>([]);
  const [draft, setDraft] = useState<MarkupPolicyConfig>(BLANK);
  const [label, setLabel] = useState("");
  const [activateNew, setActivateNew] = useState(true);
  const [preview, setPreview] = useState<MarkupPreview | null>(null);
  const [supplier, setSupplier] = useState(400);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    opsApi
      .policies()
      .then((p) => {
        setRows(p);
        const active = p.find((x) => x.active);
        if (active?.config) setDraft(active.config);
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load policies.");
      });
  }, [onExpire]);

  useEffect(() => {
    load();
  }, [load]);

  const runPreview = useCallback(() => {
    opsApi
      .previewPolicy({ supplier_total: supplier, ticket_count: 3, draft: { ...draft, label } })
      .then(setPreview)
      .catch((e) => setError(e instanceof OpsError ? e.message : "Preview failed."));
  }, [supplier, draft, label]);

  useEffect(() => {
    const t = window.setTimeout(runPreview, 250);
    return () => window.clearTimeout(t);
  }, [runPreview]);

  const num = (k: keyof MarkupPolicyConfig) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setDraft((d) => ({ ...d, [k]: Number(e.target.value) }));

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await opsApi.createPolicy({ ...draft, label, activate: activateNew });
      setLabel("");
      load();
    } catch (e) {
      setError(e instanceof OpsError ? e.message : "Could not save policy.");
    } finally {
      setBusy(false);
    }
  };

  const activate = async (v: number) => {
    setBusy(true);
    try {
      await opsApi.activatePolicy("detoura.markup", v);
      load();
    } catch (e) {
      setError(e instanceof OpsError ? e.message : "Could not activate.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2>Commercial — markup policy</h2>
      <p className="ops-muted">
        The Detoura fee per tier. Every change is a new version and is audited;
        a booking keeps the exact version it was priced with. Basic ≤ All-in-One
        is enforced by the server regardless of what you enter here.
      </p>
      {error && <p className="ops-error">{error}</p>}

      <h3>Versions</h3>
      <table className="ops-table">
        <thead>
          <tr>
            <th>Version</th>
            <th>Label</th>
            <th>Basic</th>
            <th>All-in-One</th>
            <th>Caps</th>
            <th>Bookings priced</th>
            <th>Created</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.version} className={r.active ? "ops-row-active" : ""}>
              <td className="ops-mono">
                v{r.version} {r.active && <span className="ops-state ops-state--ok">ACTIVE</span>}
              </td>
              <td>{r.label}</td>
              <td>
                {r.config
                  ? `${pct(r.config.basic_percentage)} + ${money(r.config.basic_fixed_fee)}`
                  : "—"}
              </td>
              <td>
                {r.config
                  ? `${pct(r.config.all_in_one_percentage)} + ${money(r.config.all_in_one_fixed_fee)}`
                  : "—"}
              </td>
              <td>
                {r.config
                  ? `≤${pct(r.config.max_percentage)} / ≤${money(r.config.max_fixed_fee)} / total ${money(r.config.min_total_fee)}–${money(r.config.max_total_fee)}`
                  : "—"}
              </td>
              <td>{r.bookings_priced}</td>
              <td>{when(r.created_at)}</td>
              <td>
                {!r.active && (
                  <button
                    className="ops-action"
                    disabled={busy}
                    onClick={() => activate(r.version)}
                  >
                    Activate
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>New version</h3>
      <div className="ops-form">
        <label>
          Label
          <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Q3 pricing" />
        </label>
        <fieldset>
          <legend>Basic</legend>
          <label>
            Percentage (0–1)
            <input type="number" step="0.005" value={draft.basic_percentage} onChange={num("basic_percentage")} />
          </label>
          <label>
            Fixed fee
            <input type="number" step="1" value={draft.basic_fixed_fee} onChange={num("basic_fixed_fee")} />
          </label>
        </fieldset>
        <fieldset>
          <legend>All-in-One</legend>
          <label>
            Percentage (0–1)
            <input type="number" step="0.005" value={draft.all_in_one_percentage} onChange={num("all_in_one_percentage")} />
          </label>
          <label>
            Fixed fee
            <input type="number" step="1" value={draft.all_in_one_fixed_fee} onChange={num("all_in_one_fixed_fee")} />
          </label>
        </fieldset>
        <fieldset>
          <legend>Caps</legend>
          <label>
            Max percentage
            <input type="number" step="0.01" value={draft.max_percentage} onChange={num("max_percentage")} />
          </label>
          <label>
            Max fixed fee
            <input type="number" step="1" value={draft.max_fixed_fee} onChange={num("max_fixed_fee")} />
          </label>
          <label>
            Min total fee
            <input type="number" step="1" value={draft.min_total_fee} onChange={num("min_total_fee")} />
          </label>
          <label>
            Max total fee
            <input type="number" step="1" value={draft.max_total_fee} onChange={num("max_total_fee")} />
          </label>
        </fieldset>
        <label className="ops-check">
          <input type="checkbox" checked={activateNew} onChange={(e) => setActivateNew(e.target.checked)} />
          Activate immediately
        </label>
        <button className="ops-btn" disabled={busy} onClick={save}>
          Save new version
        </button>
      </div>

      <h3>Preview</h3>
      <label className="ops-inline">
        Supplier fare
        <input
          type="number"
          step="10"
          value={supplier}
          onChange={(e) => setSupplier(Number(e.target.value))}
        />
        · 3 tickets
      </label>
      {preview && (
        <>
          <p className={preview.invariant_ok ? "ops-muted" : "ops-error"}>
            {preview.invariant_ok
              ? "✓ All-in-One ≥ Basic"
              : "✗ invariant would be violated (server would correct it)"}
          </p>
          <table className="ops-table ops-table--compact">
            <thead>
              <tr>
                <th>Tier</th>
                <th>Supplier</th>
                <th>Service fee</th>
                <th>Markup</th>
                <th>Detoura fee</th>
                <th>Customer total</th>
                <th>Bounded?</th>
              </tr>
            </thead>
            <tbody>
              {preview.lines.map((l) => (
                <tr key={l.tier}>
                  <td>{l.tier}</td>
                  <td>{money(l.supplier_total)}</td>
                  <td>{money(l.detoura_service_fee)}</td>
                  <td>{money(l.detoura_markup)}</td>
                  <td>
                    <b>{money(l.detoura_fee_total)}</b>
                  </td>
                  <td>{money(l.customer_total)}</td>
                  <td>{l.bounded ? "yes (capped)" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

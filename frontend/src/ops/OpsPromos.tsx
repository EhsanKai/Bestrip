import { useCallback, useEffect, useState } from "react";
import { opsApi, OpsError, type OpsPromo, type OpsPromoDetail } from "./opsApi";
import { money, when } from "./opsFormat";

const NEW: Record<string, unknown> = {
  code: "",
  label: "",
  enabled: true,
  kind: "PERCENTAGE",
  value: 10,
  currency: "EUR",
  target: "DETOURA_FEE",
  per_user_limit: 1,
  eligible_tiers: [],
};

export function PromosView({ onExpire }: { onExpire: () => void }) {
  const [rows, setRows] = useState<OpsPromo[]>([]);
  const [selected, setSelected] = useState<OpsPromoDetail | null>(null);
  const [form, setForm] = useState<Record<string, unknown>>(NEW);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    opsApi
      .promos()
      .then(setRows)
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load promos.");
      });
  }, [onExpire]);

  useEffect(() => {
    load();
  }, [load]);

  const open = (code: string) => {
    opsApi.promo(code).then((d) => {
      setSelected(d);
      setForm({
        code: d.code,
        label: d.label,
        enabled: d.enabled,
        kind: d.kind,
        value: d.value,
        currency: d.currency,
        target: d.target,
        starts_at: d.starts_at ?? "",
        ends_at: d.ends_at ?? "",
        global_limit: d.global_limit ?? "",
        per_user_limit: d.per_user_limit ?? "",
        min_order_value: d.min_order_value,
        max_discount: d.max_discount ?? "",
        eligible_tiers: d.eligible_tiers,
      });
    });
  };

  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async () => {
    setBusy(true);
    setError("");
    const clean: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(form)) {
      if (v === "" || v === null) continue;
      clean[k] = v;
    }
    // numeric coercions
    for (const k of ["value", "min_order_value", "max_discount", "global_limit", "per_user_limit"]) {
      if (clean[k] !== undefined) clean[k] = Number(clean[k]);
    }
    try {
      const d = await opsApi.upsertPromo(clean);
      setSelected(d);
      load();
    } catch (e) {
      setError(e instanceof OpsError ? e.message : "Could not save promo.");
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (code: string, enabled: boolean) => {
    try {
      await opsApi.setPromoEnabled(code, enabled);
      load();
      if (selected?.code === code) open(code);
    } catch (e) {
      setError(e instanceof OpsError ? e.message : "Could not update.");
    }
  };

  const tierToggle = (t: string) => {
    const cur = (form.eligible_tiers as string[]) || [];
    set("eligible_tiers", cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]);
  };

  return (
    <section>
      <h2>Promos</h2>
      <p className="ops-muted">
        A promo only ever reduces Detoura's own fee — never the supplier fare —
        and never past zero. Every create/edit/enable/disable is audited.
      </p>
      {error && <p className="ops-error">{error}</p>}

      <table className="ops-table">
        <thead>
          <tr>
            <th>Code</th>
            <th>Discount</th>
            <th>Window</th>
            <th>Limits</th>
            <th>Tiers</th>
            <th>Redemptions</th>
            <th>Revenue impact</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.code} onClick={() => open(p.code)}>
              <td className="ops-mono">
                {p.code}{" "}
                <span className={`ops-state ops-state--${p.enabled ? "ok" : "bad"}`}>
                  {p.enabled ? "ON" : "OFF"}
                </span>
              </td>
              <td>
                {p.kind === "PERCENTAGE" ? `${p.value}%` : money(p.value, p.currency)} off{" "}
                {p.target === "DETOURA_FEE" ? "Detoura fee" : "order"}
              </td>
              <td className="ops-small">
                {p.starts_at ? when(p.starts_at) : "—"} → {p.ends_at ? when(p.ends_at) : "—"}
              </td>
              <td className="ops-small">
                {p.global_limit ?? "∞"} global · {p.per_user_limit ?? "∞"}/user
                {p.min_order_value ? ` · min ${money(p.min_order_value, p.currency)}` : ""}
              </td>
              <td>{p.eligible_tiers.length ? p.eligible_tiers.join(", ") : "all"}</td>
              <td>{p.redemptions}</td>
              <td>{money(p.revenue_impact, p.currency)}</td>
              <td onClick={(e) => e.stopPropagation()}>
                <button className="ops-action" onClick={() => toggle(p.code, !p.enabled)}>
                  {p.enabled ? "Disable" : "Enable"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>{form.code ? `Edit ${form.code}` : "New promo"}</h3>
      <div className="ops-form">
        <label>
          Code
          <input value={String(form.code)} onChange={(e) => set("code", e.target.value.toUpperCase())} />
        </label>
        <label>
          Label
          <input value={String(form.label ?? "")} onChange={(e) => set("label", e.target.value)} />
        </label>
        <label>
          Kind
          <select value={String(form.kind)} onChange={(e) => set("kind", e.target.value)}>
            <option value="PERCENTAGE">Percentage</option>
            <option value="FIXED">Fixed amount</option>
          </select>
        </label>
        <label>
          Value {form.kind === "PERCENTAGE" ? "(%)" : "(amount)"}
          <input type="number" step="1" value={String(form.value)} onChange={(e) => set("value", e.target.value)} />
        </label>
        <label>
          Applies to
          <select value={String(form.target)} onChange={(e) => set("target", e.target.value)}>
            <option value="DETOURA_FEE">Detoura fee</option>
            <option value="ORDER_TOTAL">Order total</option>
          </select>
        </label>
        <label>
          Min order value
          <input type="number" step="1" value={String(form.min_order_value ?? "")} onChange={(e) => set("min_order_value", e.target.value)} />
        </label>
        <label>
          Max discount
          <input type="number" step="1" value={String(form.max_discount ?? "")} onChange={(e) => set("max_discount", e.target.value)} />
        </label>
        <label>
          Global limit
          <input type="number" step="1" value={String(form.global_limit ?? "")} onChange={(e) => set("global_limit", e.target.value)} />
        </label>
        <label>
          Per-user limit
          <input type="number" step="1" value={String(form.per_user_limit ?? "")} onChange={(e) => set("per_user_limit", e.target.value)} />
        </label>
        <label>
          Starts at
          <input type="datetime-local" value={String(form.starts_at ?? "").slice(0, 16)} onChange={(e) => set("starts_at", e.target.value)} />
        </label>
        <label>
          Ends at
          <input type="datetime-local" value={String(form.ends_at ?? "").slice(0, 16)} onChange={(e) => set("ends_at", e.target.value)} />
        </label>
        <div className="ops-check-group">
          Eligible tiers:
          {["BASIC", "ALL_IN_ONE"].map((t) => (
            <label key={t} className="ops-check">
              <input
                type="checkbox"
                checked={((form.eligible_tiers as string[]) || []).includes(t)}
                onChange={() => tierToggle(t)}
              />
              {t}
            </label>
          ))}
          <span className="ops-muted">(none = all tiers)</span>
        </div>
        <button className="ops-btn" disabled={busy} onClick={submit}>
          {form.code && rows.some((r) => r.code === form.code) ? "Save changes" : "Create promo"}
        </button>
        <button
          className="ops-action"
          onClick={() => {
            setForm(NEW);
            setSelected(null);
          }}
        >
          Clear form
        </button>
      </div>

      {selected && selected.redemption_log.length > 0 && (
        <>
          <h3>{selected.code} redemptions</h3>
          <table className="ops-table ops-table--compact">
            <thead>
              <tr>
                <th>Booking</th>
                <th>Discount</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {selected.redemption_log.map((r, i) => (
                <tr key={i}>
                  <td className="ops-mono">{r.booking_id}</td>
                  <td>{money(r.discount_amount, r.currency)}</td>
                  <td>{when(r.redeemed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

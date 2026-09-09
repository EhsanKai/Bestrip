import { useCallback, useEffect, useState } from "react";
import {
  opsApi,
  OpsError,
  type OpsBookingDetail,
  type OpsBookingSummary,
} from "./opsApi";
import {
  AuditTable,
  money,
  RecoveryTag,
  StateTag,
  unknownOrMoney,
  when,
} from "./opsFormat";

const PHASES = [
  "",
  "awaiting_travelers",
  "awaiting_confirmation",
  "revalidating",
  "reconfirm_required",
  "issuing",
  "complete",
  "partial_failure",
  "failed",
];

export function BookingsView({ onExpire }: { onExpire: () => void }) {
  const [rows, setRows] = useState<OpsBookingSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [phase, setPhase] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .bookings({ phase: phase || undefined, search: search || undefined })
      .then((p) => {
        setRows(p.bookings);
        setCounts(p.counts_by_phase);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load bookings.");
      });
  }, [phase, search, onExpire]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 6000);
    return () => window.clearInterval(t);
  }, [load]);

  if (selected) {
    return (
      <BookingDetailPanel
        bookingId={selected}
        onBack={() => setSelected(null)}
        onExpire={onExpire}
      />
    );
  }

  return (
    <section>
      <div className="ops-toolbar">
        <input
          className="ops-search"
          placeholder="Search reference, id, name or email…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={phase} onChange={(e) => setPhase(e.target.value)}>
          {PHASES.map((p) => (
            <option key={p} value={p}>
              {p ? p.replace(/_/g, " ") : "All phases"}
              {p && counts[p] ? ` (${counts[p]})` : ""}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="ops-error">{error}</p>}
      <table className="ops-table">
        <thead>
          <tr>
            <th>Journey</th>
            <th>Created</th>
            <th>Route</th>
            <th>Traveller</th>
            <th>Tier</th>
            <th>Phase</th>
            <th>Tickets</th>
            <th>Customer</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((b) => (
            <tr key={b.booking_id} onClick={() => setSelected(b.booking_id)}>
              <td className="ops-mono">{b.journey_reference}</td>
              <td>{when(b.created_at)}</td>
              <td>{b.route_cities.join(" → ")}</td>
              <td>
                {b.lead_name || <span className="ops-muted">—</span>}
                {b.lead_email ? (
                  <div className="ops-muted ops-small">{b.lead_email}</div>
                ) : null}
              </td>
              <td>{b.service_tier}</td>
              <td>
                {b.phase_label} <RecoveryTag state={b.recovery_state} />
              </td>
              <td>
                {b.confirmed_count}/{b.ticket_count}
              </td>
              <td>{money(b.customer_total ?? b.discovered_total, b.currency)}</td>
              <td>›</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={9} className="ops-muted">
                No bookings.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

const TERMINAL = new Set(["complete", "partial_failure", "failed"]);

export function BookingDetailPanel({
  bookingId,
  onBack,
  onExpire,
}: {
  bookingId: string;
  onBack: () => void;
  onExpire: () => void;
}) {
  const [d, setD] = useState<OpsBookingDetail | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .booking(bookingId)
      .then((x) => {
        setD(x);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load booking.");
      });
  }, [bookingId, onExpire]);

  useEffect(() => {
    load();
    const t = window.setInterval(() => {
      if (!d || !TERMINAL.has(d.phase)) load();
    }, 4000);
    return () => window.clearInterval(t);
  }, [load, d]);

  if (error) return <p className="ops-error">{error}</p>;
  if (!d) return <p className="ops-muted">Loading…</p>;

  const e = d.economics;

  return (
    <section className="ops-detail">
      <button className="ops-link" onClick={onBack}>
        ‹ All bookings
      </button>

      <div className="ops-detail__head">
        <div>
          <h2 className="ops-mono">{d.journey_reference}</h2>
          <p className="ops-muted">
            {d.route_cities.join(" → ")} · {d.trip_label}
          </p>
        </div>
        <div className="ops-detail__meta">
          <span className={`ops-pill ops-pill--${d.mode === "sandbox_booked" ? "sandbox" : "demo"}`}>
            {d.mode}
          </span>
          <span className="ops-pill">{d.phase_label}</span>
          <RecoveryTag state={d.recovery_state} />
        </div>
      </div>

      <dl className="ops-kv">
        <div><dt>Booking id</dt><dd className="ops-mono">{d.booking_id}</dd></div>
        <div><dt>Session</dt><dd className="ops-mono">{d.session_ref || "—"}</dd></div>
        <div><dt>Created</dt><dd>{when(d.created_at)}</dd></div>
        <div><dt>Updated</dt><dd>{when(d.updated_at)}</dd></div>
        <div><dt>Traveller</dt><dd>{d.lead_name || "—"}</dd></div>
        <div><dt>Email</dt><dd>{d.lead_email || "—"}</dd></div>
        <div><dt>Travellers</dt><dd>{d.party_size}</dd></div>
        <div><dt>Service tier</dt><dd>{d.service_tier}</dd></div>
        <div><dt>Discovered total</dt><dd>{money(d.discovered_total, d.currency)}</dd></div>
        <div><dt>Current total</dt><dd>{money(d.current_total, d.currency)}</dd></div>
        <div><dt>Customer price</dt><dd>{money(d.customer_total, d.currency)}</dd></div>
      </dl>
      {d.reconfirm_note && (
        <p className="ops-note">Reconfirm note: {d.reconfirm_note}</p>
      )}

      <h3>Tickets ({d.confirmed_count}/{d.ticket_count} confirmed)</h3>
      <div className="ops-tickets">
        {d.items.map((it) => (
          <div className="ops-ticket" key={it.sequence}>
            <div className="ops-ticket__head">
              <b>
                #{it.sequence} · {it.origin_airport} → {it.destination_airport}
              </b>
              <StateTag state={it.state} />
              {!it.required && <span className="ops-pill">optional</span>}
            </div>
            <dl className="ops-kv ops-kv--tight">
              <div><dt>Departs</dt><dd>{when(it.departure)}</dd></div>
              <div><dt>Arrives</dt><dd>{when(it.arrival)}</dd></div>
              <div><dt>Carrier</dt><dd>{it.carrier || "—"} {it.flight_number}</dd></div>
              <div><dt>Provider</dt><dd>{it.provider || "—"}</dd></div>
              <div><dt>Offer id</dt><dd className="ops-mono ops-ellipsis">{it.offer_id || "—"}</dd></div>
              <div><dt>Duffel order</dt><dd className="ops-mono">{it.duffel_order_id || "—"}</dd></div>
              <div><dt>Quoted</dt><dd>{money(it.quoted_price, it.currency)}</dd></div>
              <div><dt>Revalidated</dt><dd>{money(it.current_price, it.currency)}</dd></div>
              <div><dt>Booked</dt><dd>{money(it.booked_price, it.currency)}</dd></div>
              <div><dt>Cabin bag</dt><dd>{it.cabin_baggage}</dd></div>
              <div><dt>Checked bag</dt><dd>{it.checked_baggage}</dd></div>
            </dl>
            {it.detail && <p className="ops-ticket__detail">{it.detail}</p>}
            <div className="ops-actions">
              {it.actions.map((a) => (
                <button
                  key={a.action}
                  className="ops-action"
                  disabled={!a.enabled}
                  title={a.reason || undefined}
                >
                  {a.action.replace(/_/g, " ")}
                  {!a.enabled && <span className="ops-action__na">not implemented</span>}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

      <h3>Economics</h3>
      {e ? (
        <dl className="ops-kv">
          <div><dt>Supplier cost</dt><dd>{money(e.supplier_cost, e.currency)}</dd></div>
          <div><dt>Detoura service fee</dt><dd>{money(e.detoura_service_fee, e.currency)}</dd></div>
          <div><dt>Detoura markup</dt><dd>{money(e.detoura_markup, e.currency)}</dd></div>
          <div><dt>Discount</dt><dd>{e.discount ? `−${money(e.discount, e.currency)}` : "—"}</dd></div>
          <div><dt>Customer price</dt><dd>{money(e.customer_price, e.currency)}</dd></div>
          <div><dt>Detoura gross revenue</dt><dd>{money(e.detoura_gross_revenue, e.currency)}</dd></div>
          <div><dt>Markup policy</dt><dd className="ops-mono">{e.markup_policy}</dd></div>
          <div><dt>Promo</dt><dd>{e.promo_code || "—"}</dd></div>
          <div><dt>Provider/API cost</dt><dd>{unknownOrMoney(e.provider_cost_estimate, e.currency)}</dd></div>
          <div><dt>Payment cost</dt><dd>{unknownOrMoney(e.payment_cost, e.currency)}</dd></div>
          <div><dt>Refund</dt><dd>{unknownOrMoney(e.refund, e.currency)}</dd></div>
          <div><dt>Recovery cost</dt><dd>{unknownOrMoney(e.recovery_cost, e.currency)}</dd></div>
          <div>
            <dt>Contribution margin</dt>
            <dd>
              {e.contribution_margin === null ? (
                <span className="ops-unknown">
                  not computable — some costs UNKNOWN
                </span>
              ) : (
                money(e.contribution_margin, e.currency)
              )}
            </dd>
          </div>
        </dl>
      ) : (
        <p className="ops-muted">
          No economics ledger row yet — written when the journey reaches a
          terminal state.
        </p>
      )}

      <h3>Audit trail</h3>
      <AuditTable rows={d.audit} />
    </section>
  );
}

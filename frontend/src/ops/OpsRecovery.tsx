import { useCallback, useEffect, useState } from "react";
import { opsApi, OpsError, type OpsRecoveryPage } from "./opsApi";
import { BookingDetailPanel } from "./OpsBookings";
import { RecoveryTag, when } from "./opsFormat";

const EXPLAIN: Record<string, string> = {
  PRICE_CHANGED:
    "A fare moved past tolerance during revalidation. The journey is NOT booked; it awaits a fresh confirmation.",
  PARTIAL_FAILURE:
    "Some tickets were secured and at least one required ticket was not. The journey is NOT a completed booking.",
  RECOVERY_REQUIRED: "The journey needs operator attention before it can proceed.",
  UNAVAILABLE: "A leg became unavailable or expired before it could be issued.",
  CANCELLATION_FAILED: "A cancellation was attempted and did not complete.",
  CHANGE_REQUIRES_ACTION: "A change request needs an operator decision.",
  FAILED: "No ticket was secured. The journey did not book.",
};

export function RecoveryView({ onExpire }: { onExpire: () => void }) {
  const [page, setPage] = useState<OpsRecoveryPage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .recovery()
      .then((p) => {
        setPage(p);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load recovery queue.");
      });
  }, [onExpire]);

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
      <h2>Recovery Center</h2>
      <p className="ops-muted">
        Journeys that stopped short of a clean booking. None of these appears to
        a customer as “booked”. Open one to run the recovery workflow on a
        ticket — inspect → revalidate → find replacement → compare → approve →
        execute. Executing records the approved decision; it never auto-books.
      </p>
      {error && <p className="ops-error">{error}</p>}

      {page && Object.keys(page.by_state).length > 0 && (
        <div className="ops-chips">
          {Object.entries(page.by_state).map(([s, n]) => (
            <span key={s} className="ops-chip">
              {s} <b>{n}</b>
            </span>
          ))}
        </div>
      )}

      <table className="ops-table">
        <thead>
          <tr>
            <th>Journey</th>
            <th>State</th>
            <th>Route</th>
            <th>Traveller</th>
            <th>Tickets</th>
            <th>Updated</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {(page?.items ?? []).map((b) => (
            <tr key={b.booking_id} onClick={() => setSelected(b.booking_id)}>
              <td className="ops-mono">{b.journey_reference}</td>
              <td>
                <RecoveryTag state={b.recovery_state} />
                <div className="ops-muted ops-small">
                  {EXPLAIN[b.recovery_state]}
                </div>
              </td>
              <td>{b.route_cities.join(" → ")}</td>
              <td>{b.lead_name || "—"}</td>
              <td>
                {b.confirmed_count}/{b.ticket_count}
              </td>
              <td>{when(b.updated_at)}</td>
              <td>›</td>
            </tr>
          ))}
          {page && page.items.length === 0 && (
            <tr>
              <td colSpan={7} className="ops-muted">
                Nothing needs recovery. 🎉
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

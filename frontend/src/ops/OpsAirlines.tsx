import { useCallback, useEffect, useState } from "react";
import { opsApi, OpsError, type AirlineReport } from "./opsApi";
import { money } from "./opsFormat";

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

export function AirlinesView({ onExpire }: { onExpire: () => void }) {
  const [dimension, setDimension] = useState<"marketing" | "operating">("marketing");
  const [report, setReport] = useState<AirlineReport | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .airlines({ dimension })
      .then((r) => {
        setReport(r);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load airline report.");
      });
  }, [dimension, onExpire]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <section>
      <h2>Airline performance</h2>
      <p className="ops-muted">
        Aggregated from the persisted booking record. Marketing (ticketed) and
        operating (flown) carriers are reported as separate dimensions — never
        merged. Margin figures cover only bookings whose costs are fully known;
        the rest stay UNKNOWN, never zero.
      </p>

      <div className="ops-toolbar">
        <div className="ops-seg">
          {(["marketing", "operating"] as const).map((d) => (
            <button
              key={d}
              className={dimension === d ? "is-on" : ""}
              onClick={() => setDimension(d)}
            >
              {d}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="ops-error">{error}</p>}

      <table className="ops-table">
        <thead>
          <tr>
            <th>Airline</th>
            <th>Tickets</th>
            <th>Issued</th>
            <th>Failed</th>
            <th>Issue-fail rate</th>
            <th>Bookings</th>
            <th>Supplier spend</th>
            <th>Avg fare</th>
            <th>Detoura revenue</th>
            <th>Margin (known)</th>
            <th>Cancels</th>
            <th>Changes</th>
            <th>Recoveries</th>
          </tr>
        </thead>
        <tbody>
          {(report?.airlines ?? []).map((a) => (
            <tr key={a.iata_code}>
              <td>
                <span className="ops-airline">
                  <span className="ops-airline__code">{a.iata_code}</span>
                  {a.name}
                </span>
              </td>
              <td>{a.tickets_total}</td>
              <td>{a.tickets_issued}</td>
              <td>{a.tickets_failed}</td>
              <td>{pct(a.issuance_failure_rate)}</td>
              <td>{a.bookings}</td>
              <td>{money(a.supplier_spend, a.currency)}</td>
              <td>{a.avg_fare === null ? "—" : money(a.avg_fare, a.currency)}</td>
              <td>{money(a.detoura_revenue_gross, a.currency)}</td>
              <td>
                {a.detoura_margin_known === null ? (
                  <span className="ops-unknown">UNKNOWN</span>
                ) : (
                  money(a.detoura_margin_known, a.currency)
                )}
              </td>
              <td>{a.cancellations || "—"}</td>
              <td>{a.changes || "—"}</td>
              <td>{a.recoveries || "—"}</td>
            </tr>
          ))}
          {report && report.airlines.length === 0 && (
            <tr>
              <td colSpan={13} className="ops-muted">
                No issued tickets yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

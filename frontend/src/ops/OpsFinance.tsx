import { useCallback, useEffect, useState } from "react";
import { opsApi, OpsError, type FinanceSummary } from "./opsApi";
import { money } from "./opsFormat";

type Range = "today" | "7d" | "30d" | "all";

function windowFor(r: Range): { since?: string } {
  if (r === "all") return {};
  const now = new Date();
  const d = new Date(now);
  if (r === "today") d.setHours(0, 0, 0, 0);
  if (r === "7d") d.setDate(d.getDate() - 7);
  if (r === "30d") d.setDate(d.getDate() - 30);
  return { since: d.toISOString() };
}

function Unknown({ n }: { n: number }) {
  if (n === 0) return null;
  return <span className="ops-unknown"> · {n} UNKNOWN</span>;
}

export function FinanceView({ onExpire }: { onExpire: () => void }) {
  const [range, setRange] = useState<Range>("30d");
  const [f, setF] = useState<FinanceSummary | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .finance(windowFor(range))
      .then((x) => {
        setF(x);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load finance.");
      });
  }, [range, onExpire]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 20000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <section>
      <h2>
        Finance <span className="ops-pill ops-pill--test">TEST DATA</span>
      </h2>
      <div className="ops-toolbar">
        {(["today", "7d", "30d", "all"] as Range[]).map((r) => (
          <button
            key={r}
            className={`ops-chip ${range === r ? "is-on" : ""}`}
            onClick={() => setRange(r)}
          >
            {r === "all" ? "All time" : r === "today" ? "Today" : `Last ${r}`}
          </button>
        ))}
      </div>
      {error && <p className="ops-error">{error}</p>}

      {f && (
        <>
          <div className="ops-cards">
            <div className="ops-metric">
              <span>Bookings</span>
              <b>{f.bookings}</b>
            </div>
            <div className="ops-metric">
              <span>Gross booking value</span>
              <b>{money(f.gross_booking_value, f.currency)}</b>
            </div>
            <div className="ops-metric">
              <span>Supplier cost</span>
              <b>{money(f.supplier_cost, f.currency)}</b>
            </div>
            <div className="ops-metric">
              <span>Detoura revenue (gross)</span>
              <b>{money(f.detoura_revenue_gross, f.currency)}</b>
            </div>
            <div className="ops-metric">
              <span>Promo discounts</span>
              <b>−{money(f.promo_discounts, f.currency)}</b>
            </div>
            <div className="ops-metric">
              <span>Detoura revenue (net)</span>
              <b>{money(f.detoura_revenue_net, f.currency)}</b>
            </div>
            <div className="ops-metric">
              <span>Avg Detoura fee / booking</span>
              <b>{money(f.avg_detoura_fee, f.currency)}</b>
            </div>
            <div className="ops-metric">
              <span>Avg margin / booking</span>
              <b>
                {f.avg_margin_known === null
                  ? "—"
                  : money(f.avg_margin_known, f.currency)}
              </b>
              {f.avg_margin_excluded_bookings > 0 && (
                <small className="ops-unknown">
                  {f.avg_margin_excluded_bookings} excluded (unknown costs)
                </small>
              )}
            </div>
          </div>

          <h3>Costs & contribution</h3>
          <dl className="ops-kv">
            <div>
              <dt>Provider / API cost</dt>
              <dd>
                {money(f.provider_cost_estimate_known, f.currency)}
                <Unknown n={f.provider_cost_estimate_unknown_bookings} />
              </dd>
            </div>
            <div>
              <dt>Payment cost</dt>
              <dd>
                {money(f.payment_cost_known, f.currency)}
                <Unknown n={f.payment_cost_unknown_bookings} />
              </dd>
            </div>
            <div>
              <dt>Refunds</dt>
              <dd>
                {money(f.refund_known, f.currency)}
                <Unknown n={f.refund_unknown_bookings} />
              </dd>
            </div>
            <div>
              <dt>Recovery cost</dt>
              <dd>
                {money(f.recovery_cost_known, f.currency)}
                <Unknown n={f.recovery_cost_unknown_bookings} />
              </dd>
            </div>
            <div>
              <dt>Gross contribution (fully-costed bookings)</dt>
              <dd>
                <b>{money(f.gross_contribution_known, f.currency)}</b>
                {f.bookings_with_unknown_costs > 0 && (
                  <span className="ops-unknown">
                    {" "}
                    — excludes {f.bookings_with_unknown_costs} with UNKNOWN costs
                  </span>
                )}
              </dd>
            </div>
          </dl>

          <h3>Basic vs All-in-One</h3>
          <table className="ops-table">
            <thead>
              <tr>
                <th>Tier</th>
                <th>Bookings</th>
                <th>Tier selected</th>
                <th>Conversion</th>
                <th>Gross value</th>
                <th>Detoura revenue (net)</th>
                <th>Avg fee</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(f.by_tier).map(([tier, b]) => (
                <tr key={tier}>
                  <td>{tier}</td>
                  <td>{b.bookings}</td>
                  <td>{b.tier_selected}</td>
                  <td>
                    {b.conversion === null ? "—" : `${(b.conversion * 100).toFixed(0)}%`}
                  </td>
                  <td>{money(b.gross_booking_value, f.currency)}</td>
                  <td>{money(b.detoura_revenue_net, f.currency)}</td>
                  <td>{money(b.avg_detoura_fee, f.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="ops-muted">
            UNKNOWN costs are never counted as zero. Gross contribution and
            average margin are over bookings where every attributable cost is
            known; the rest are stated as excluded.
          </p>
        </>
      )}
    </section>
  );
}

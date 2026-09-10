import { useCallback, useEffect, useState } from "react";
import { opsApi, OpsError, type AnalyticsReport } from "./opsApi";

type Range = "today" | "7d" | "30d" | "all";

function windowFor(r: Range): { since?: string } {
  if (r === "all") return {};
  const d = new Date();
  if (r === "today") d.setHours(0, 0, 0, 0);
  if (r === "7d") d.setDate(d.getDate() - 7);
  if (r === "30d") d.setDate(d.getDate() - 30);
  return { since: d.toISOString() };
}

export function AnalyticsView({ onExpire }: { onExpire: () => void }) {
  const [range, setRange] = useState<Range>("30d");
  const [a, setA] = useState<AnalyticsReport | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .analytics(windowFor(range))
      .then((x) => {
        setA(x);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load analytics.");
      });
  }, [range, onExpire]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, [load]);

  const maxFunnel = a ? Math.max(1, ...a.funnel.map((s) => s.sessions)) : 1;

  return (
    <section>
      <h2>
        Analytics <span className="ops-pill ops-pill--test">TEST DATA</span>
      </h2>
      <p className="ops-muted">
        Anonymous product-funnel events — a random per-tab and per-browser key,
        no traveller PII. Used for product decisions, not profiling.
      </p>
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

      {a && (
        <>
          <h3>Funnel (distinct sessions)</h3>
          <div className="ops-funnel">
            {a.funnel.map((s) => (
              <div className="ops-funnel__row" key={s.stage}>
                <span className="ops-funnel__label">{s.stage}</span>
                <span className="ops-funnel__bar">
                  <span
                    style={{ width: `${(s.sessions / maxFunnel) * 100}%` }}
                  />
                </span>
                <span className="ops-funnel__n">{s.sessions}</span>
                <span className="ops-funnel__drop">
                  {s.rate_from_prev === null
                    ? ""
                    : `${(s.rate_from_prev * 100).toFixed(0)}% kept${
                        s.drop_from_prev ? ` · −${s.drop_from_prev}` : ""
                      }`}
                </span>
              </div>
            ))}
          </div>

          <div className="ops-cards">
            <div className="ops-metric">
              <span>Basic selected</span>
              <b>{a.tier_selection.BASIC?.selected ?? 0}</b>
              <small>
                {a.tier_selection.BASIC?.conversion == null
                  ? "—"
                  : `${(a.tier_selection.BASIC.conversion * 100).toFixed(0)}% booked`}
              </small>
            </div>
            <div className="ops-metric">
              <span>All-in-One selected</span>
              <b>{a.tier_selection.ALL_IN_ONE?.selected ?? 0}</b>
              <small>
                {a.tier_selection.ALL_IN_ONE?.conversion == null
                  ? "—"
                  : `${(a.tier_selection.ALL_IN_ONE.conversion * 100).toFixed(0)}% booked`}
              </small>
            </div>
            <div className="ops-metric">
              <span>Promo applied (sessions)</span>
              <b>{a.promo_impact.sessions_applied_promo}</b>
              <small>
                {a.promo_impact.conversion == null
                  ? "—"
                  : `${(a.promo_impact.conversion * 100).toFixed(0)}% booked`}
              </small>
            </div>
            <div className="ops-metric">
              <span>Repeat searchers</span>
              <b>{a.repeat_search.repeat_searchers}</b>
              <small>
                of {a.repeat_search.visitors_who_searched} ·{" "}
                {a.repeat_search.avg_searches_per_visitor} avg
              </small>
            </div>
          </div>

          <h3>Event counts</h3>
          <table className="ops-table ops-table--compact">
            <thead>
              <tr>
                <th>Event</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(a.event_counts)
                .sort((x, y) => y[1] - x[1])
                .map(([e, n]) => (
                  <tr key={e}>
                    <td>
                      <code>{e}</code>
                    </td>
                    <td>{n}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

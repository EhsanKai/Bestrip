import type { ReactElement } from "react";
import type { OpsAuditEvent } from "./opsApi";

export function money(n: number | null | undefined, ccy = "EUR"): string {
  if (n === null || n === undefined) return "—";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: ccy,
      maximumFractionDigits: 2,
    }).format(n);
  } catch {
    return `${n.toFixed(2)} ${ccy}`;
  }
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function unknownOrMoney(
  n: number | null | undefined,
  ccy: string,
): ReactElement {
  if (n === null || n === undefined)
    return <span className="ops-unknown">UNKNOWN</span>;
  return <>{money(n, ccy)}</>;
}

const STATE_CLASS: Record<string, string> = {
  CONFIRMED: "ok",
  BOOKING: "active",
  USER_CONFIRMED: "active",
  REVALIDATING: "active",
  READY: "active",
  FAILED: "bad",
  UNAVAILABLE: "bad",
  EXPIRED: "bad",
  PROVIDER_FAILURE: "bad",
  NOT_ATTEMPTED: "muted",
  DRAFT: "muted",
};

export function StateTag({ state }: { state: string }) {
  return (
    <span className={`ops-state ops-state--${STATE_CLASS[state] ?? "muted"}`}>
      {state}
    </span>
  );
}

export function RecoveryTag({ state }: { state: string }) {
  if (!state) return null;
  return <span className="ops-state ops-state--bad">{state}</span>;
}

export function AuditTable({ rows }: { rows: OpsAuditEvent[] }) {
  if (rows.length === 0)
    return <p className="ops-muted">No audit events.</p>;
  return (
    <table className="ops-table ops-table--compact">
      <thead>
        <tr>
          <th>When</th>
          <th>Actor</th>
          <th>Action</th>
          <th>Target</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((e) => (
          <tr key={e.id}>
            <td>{when(e.ts)}</td>
            <td>{e.actor}</td>
            <td>
              <code>{e.action}</code>
            </td>
            <td className="ops-mono">
              {e.target_type ? `${e.target_type}:` : ""}
              {e.target_id}
            </td>
            <td>
              {e.note}
              {e.before || e.after ? (
                <details>
                  <summary>before / after</summary>
                  <pre>
                    {JSON.stringify({ before: e.before, after: e.after }, null, 2)}
                  </pre>
                </details>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

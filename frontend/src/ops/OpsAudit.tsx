import { useCallback, useEffect, useState } from "react";
import { opsApi, OpsError, type OpsAuditEvent } from "./opsApi";
import { AuditTable } from "./opsFormat";

export function AuditView({ onExpire }: { onExpire: () => void }) {
  const [rows, setRows] = useState<OpsAuditEvent[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    opsApi
      .audit({ limit: 500 })
      .then((r) => {
        setRows(r);
        setError("");
      })
      .catch((e) => {
        if (e instanceof OpsError && e.status === 401) return onExpire();
        setError(e instanceof OpsError ? e.message : "Failed to load audit log.");
      });
  }, [onExpire]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, [load]);

  const shown = filter
    ? rows.filter((e) =>
        `${e.action} ${e.actor} ${e.target_type} ${e.target_id} ${e.note}`
          .toLowerCase()
          .includes(filter.toLowerCase()),
      )
    : rows;

  return (
    <section>
      <h2>Admin audit log</h2>
      <p className="ops-muted">
        Every sensitive ops action, with actor, target and a safe before/after
        snapshot. Secrets and traveller PII are never recorded here.
      </p>
      {error && <p className="ops-error">{error}</p>}
      <div className="ops-toolbar">
        <input
          className="ops-search"
          placeholder="Filter by action, target, note…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="ops-muted">{shown.length} events</span>
      </div>
      <AuditTable rows={shown} />
    </section>
  );
}

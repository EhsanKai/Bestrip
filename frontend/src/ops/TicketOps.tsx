import { useState } from "react";
import {
  opsApi,
  OpsError,
  type OpsBookingDetail,
  type OpsBookingItem,
  type TicketOperation,
} from "./opsApi";
import { money, when } from "./opsFormat";

/* Idempotency keys: stable per (operation, step) for the life of this panel,
 * so a double-click or a retry after a timeout cannot execute twice. */
function useIdemKey() {
  const [key] = useState(
    () => `ik_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
  );
  return key;
}

const CANCEL_TERMINAL = new Set([
  "CANCELLED", "REFUNDED", "REFUND_PENDING", "PARTIALLY_REFUNDED",
  "NON_REFUNDABLE", "CANCELLATION_FAILED",
]);

function refundLine(op: TicketOperation): string {
  const r = (op.result ?? op.quote ?? {}) as Record<string, unknown>;
  const amt = r.refund_amount as number | null | undefined;
  const ccy = (r.currency as string) || "EUR";
  const status = (r.refund_status as string) || "";
  if (op.state === "NON_REFUNDABLE") return "No refund — the provider stated nothing is due.";
  if (op.state === "REFUND_PENDING")
    return "Cancelled. Refund not yet settled by the provider (this is NOT a refund).";
  if (amt === null || amt === undefined) return "Refund amount: not stated by the provider.";
  return `Refund: ${money(amt, ccy)}${status ? ` (${status})` : ""}`;
}

export function TicketOpsPanel({
  booking,
  item,
  onChanged,
  onExpire,
}: {
  booking: OpsBookingDetail;
  item: OpsBookingItem;
  onChanged: () => void;
  onExpire: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const idem = useIdemKey();

  const ops = booking.operations.filter((o) => o.sequence === item.sequence);
  const cancelOp = ops.find((o) => o.kind === "CANCELLATION");
  const changeOp = ops.find((o) => o.kind === "CHANGE");
  const recoveryOp = ops.find(
    (o) => o.kind === "RECOVERY" && o.state !== "ABANDONED",
  );

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setErr("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      if (e instanceof OpsError && e.status === 401) return onExpire();
      setErr(e instanceof OpsError ? e.message : "Action failed.");
    } finally {
      setBusy("");
    }
  };

  const act = (a: string) => item.actions.find((x) => x.action === a);

  return (
    <div className="ops-tops">
      {err && <p className="ops-error">{err}</p>}

      {/* ---- Cancellation ---- */}
      <div className="ops-tops__block">
        <h4>Cancellation</h4>
        {!cancelOp && (
          <button
            className="ops-action"
            disabled={!act("CHECK_CANCELLATION_ELIGIBILITY")?.enabled || !!busy}
            title={act("CHECK_CANCELLATION_ELIGIBILITY")?.reason || undefined}
            onClick={() =>
              run("elig", () =>
                opsApi.ticketOp.cancellationEligibility(booking.booking_id, item.sequence),
              )
            }
          >
            {busy === "elig" ? "Checking…" : "Check cancellation eligibility"}
          </button>
        )}

        {cancelOp && cancelOp.state === "NOT_SUPPORTED_IN_DEMO" && (
          <p className="ops-muted">
            This booking created no provider order — a real cancellation cannot be
            performed.
          </p>
        )}
        {cancelOp && cancelOp.state === "INELIGIBLE" && (
          <p className="ops-note">
            Not cancellable through this channel.{" "}
            {(cancelOp.quote?.note as string) || ""}
          </p>
        )}

        {cancelOp && cancelOp.state === "ELIGIBLE" && (
          <div className="ops-tops__quote">
            <p className="ops-note">
              Consequences of cancelling — as the provider stated them:
            </p>
            <dl className="ops-kv ops-kv--tight">
              <div>
                <dt>Refund</dt>
                <dd>
                  {cancelOp.quote?.refund_amount === null ||
                  cancelOp.quote?.refund_amount === undefined
                    ? "not stated"
                    : money(
                        cancelOp.quote.refund_amount as number,
                        (cancelOp.quote.currency as string) || item.currency,
                      )}
                </dd>
              </div>
              <div>
                <dt>Refund to</dt>
                <dd>{(cancelOp.quote?.refund_to as string) || "—"}</dd>
              </div>
            </dl>
            <ApproveExecute
              op={cancelOp}
              kind="cancellation"
              idem={idem}
              busy={busy}
              onRun={run}
              executeLabel="Execute cancellation"
              confirmText="This cancels the ticket at the provider. It cannot be undone."
            />
          </div>
        )}
        {cancelOp && cancelOp.state === "APPROVED" && (
          <ApproveExecute
            op={cancelOp}
            kind="cancellation"
            idem={idem}
            busy={busy}
            onRun={run}
            executeLabel="Execute cancellation"
            confirmText="This cancels the ticket at the provider. It cannot be undone."
          />
        )}

        {cancelOp && CANCEL_TERMINAL.has(cancelOp.state) && (
          <div
            className={`ops-tops__result ${
              cancelOp.state === "CANCELLATION_FAILED" ? "is-bad" : "is-ok"
            }`}
          >
            <b>{cancelOp.state.replace(/_/g, " ")}</b>
            <p>{refundLine(cancelOp)}</p>
            <p className="ops-small ops-muted">
              {(cancelOp.result?.detail as string) || ""}
            </p>
          </div>
        )}
      </div>

      {/* ---- Change ---- */}
      <div className="ops-tops__block">
        <h4>Change / rebooking</h4>
        {!changeOp && (
          <button
            className="ops-action"
            disabled={!act("CHECK_CHANGE_CAPABILITY")?.enabled || !!busy}
            title={act("CHECK_CHANGE_CAPABILITY")?.reason || undefined}
            onClick={() =>
              run("cap", () =>
                opsApi.ticketOp.changeCapability(booking.booking_id, item.sequence),
              )
            }
          >
            {busy === "cap" ? "Checking…" : "Check change capability"}
          </button>
        )}
        {changeOp && (
          <div className="ops-tops__quote">
            {changeOp.state === "NOT_SUPPORTED" ||
            changeOp.state === "NOT_SUPPORTED_IN_DEMO" ? (
              <p className="ops-note">
                <b>NOT SUPPORTED.</b>{" "}
                {(changeOp.quote?.note as string) ||
                  "The provider does not offer a change for this order."}
              </p>
            ) : (
              <>
                <p className="ops-note">
                  Capability: <b>{(changeOp.quote?.capability as string) || "—"}</b>.
                  A priced change offer needs the replacement itinerary
                  constructed against the order — do that through the provider,
                  then attach the change offer id. This console does not fake a
                  change it cannot price.
                </p>
                {changeOp.state === "FAILED" && changeOp.result && (
                  <p className="ops-note is-bad">
                    {changeOp.result.detail as string}
                  </p>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* ---- Recovery ---- */}
      <div className="ops-tops__block">
        <h4>Recovery</h4>
        {!recoveryOp && (
          <RecoveryStart
            bookingId={booking.booking_id}
            seq={item.sequence}
            disabled={!act("START_RECOVERY")?.enabled || !!busy}
            reason={act("START_RECOVERY")?.reason}
            busy={busy === "rec-start"}
            onRun={run}
          />
        )}
        {recoveryOp && (
          <RecoveryWorkflow
            op={recoveryOp}
            item={item}
            idem={idem}
            busy={busy}
            onRun={run}
          />
        )}
      </div>
    </div>
  );
}

function ApproveExecute({
  op,
  kind,
  idem,
  busy,
  onRun,
  executeLabel,
  confirmText,
}: {
  op: TicketOperation;
  kind: "cancellation" | "change" | "recovery";
  idem: string;
  busy: string;
  onRun: (l: string, fn: () => Promise<unknown>) => Promise<void>;
  executeLabel: string;
  confirmText: string;
}) {
  const [reason, setReason] = useState("");
  const approved = op.state === "APPROVED";
  return (
    <div className="ops-tops__ae">
      {!approved ? (
        <>
          <input
            className="ops-search"
            placeholder="Reason (recorded in the audit trail)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            className="ops-action ops-action--warn"
            disabled={!!busy}
            onClick={() =>
              onRun("approve", () =>
                opsApi.ticketOp.step(op.operation_id, kind, "approve", { reason }),
              )
            }
          >
            {busy === "approve" ? "Approving…" : "Approve"}
          </button>
        </>
      ) : (
        <button
          className="ops-action ops-action--danger"
          disabled={!!busy}
          onClick={() => {
            if (!window.confirm(confirmText)) return;
            onRun("execute", () =>
              opsApi.ticketOp.step(op.operation_id, kind, "execute", {
                idempotency_key: idem,
              }),
            );
          }}
        >
          {busy === "execute" ? "Executing…" : executeLabel}
        </button>
      )}
    </div>
  );
}

function RecoveryStart({
  bookingId,
  seq,
  disabled,
  reason,
  busy,
  onRun,
}: {
  bookingId: string;
  seq: number;
  disabled: boolean;
  reason?: string;
  busy: boolean;
  onRun: (l: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [why, setWhy] = useState("");
  return (
    <div className="ops-tops__ae">
      <input
        className="ops-search"
        placeholder="Why does this need recovery?"
        value={why}
        onChange={(e) => setWhy(e.target.value)}
      />
      <button
        className="ops-action"
        disabled={disabled || !why.trim()}
        title={reason || undefined}
        onClick={() =>
          onRun("rec-start", () =>
            opsApi.ticketOp.startRecovery(bookingId, seq, why.trim()),
          )
        }
      >
        {busy ? "Starting…" : "Start recovery"}
      </button>
    </div>
  );
}

function RecoveryWorkflow({
  op,
  item,
  idem,
  busy,
  onRun,
}: {
  op: TicketOperation;
  item: OpsBookingItem;
  idem: string;
  busy: string;
  onRun: (l: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [c, setC] = useState({
    summary: "",
    new_route: `${item.origin_airport}→${item.destination_airport}`,
    new_departure: "",
    carrier: "",
    flight_number: "",
    supplier_fare: "",
    cabin_baggage: "",
    checked_baggage: "",
    connection_note: "",
  });
  const set = (k: string, v: string) => setC((p) => ({ ...p, [k]: v }));
  const cand = op.quote?.candidate as Record<string, unknown> | undefined;

  return (
    <div className="ops-tops__quote">
      <p className="ops-note">
        Recovery state: <b>{op.state}</b>. INSPECT → REVALIDATE → FIND
        REPLACEMENT → COMPARE → APPROVE → EXECUTE. Executing never auto-books —
        it records the approved decision.
      </p>

      {["INSPECTED", "REVALIDATED"].includes(op.state) && (
        <div className="ops-tops__cand">
          <div className="ops-tops__grid">
            <label>
              Summary
              <input value={c.summary} onChange={(e) => set("summary", e.target.value)} />
            </label>
            <label>
              New route
              <input value={c.new_route} onChange={(e) => set("new_route", e.target.value)} />
            </label>
            <label>
              New departure
              <input
                value={c.new_departure}
                placeholder="2026-11-02T09:00"
                onChange={(e) => set("new_departure", e.target.value)}
              />
            </label>
            <label>
              Carrier
              <input value={c.carrier} onChange={(e) => set("carrier", e.target.value)} />
            </label>
            <label>
              Flight number
              <input
                value={c.flight_number}
                onChange={(e) => set("flight_number", e.target.value)}
              />
            </label>
            <label>
              Supplier fare
              <input
                value={c.supplier_fare}
                onChange={(e) => set("supplier_fare", e.target.value)}
              />
            </label>
            <label>
              Cabin baggage
              <input
                value={c.cabin_baggage}
                onChange={(e) => set("cabin_baggage", e.target.value)}
              />
            </label>
            <label>
              Checked baggage
              <input
                value={c.checked_baggage}
                onChange={(e) => set("checked_baggage", e.target.value)}
              />
            </label>
            <label>
              Connection note
              <input
                value={c.connection_note}
                onChange={(e) => set("connection_note", e.target.value)}
              />
            </label>
          </div>
          <button
            className="ops-action"
            disabled={!!busy || !c.summary.trim()}
            onClick={() =>
              onRun("cand", () =>
                opsApi.ticketOp.step(op.operation_id, "recovery", "candidate", {
                  ...c,
                  supplier_fare: c.supplier_fare ? Number(c.supplier_fare) : null,
                }),
              )
            }
          >
            {busy === "cand" ? "Saving…" : "Record replacement for comparison"}
          </button>
        </div>
      )}

      {cand && (
        <div className="ops-tops__compare">
          <div>
            <b>Original</b>
            <p>
              {item.origin_airport} → {item.destination_airport}
              <br />
              {when(item.departure)} · {item.carrier} {item.flight_number}
              <br />
              {money(item.booked_price ?? item.current_price ?? item.quoted_price, item.currency)}
              <br />
              cabin {item.cabin_baggage} · checked {item.checked_baggage}
            </p>
          </div>
          <div>
            <b>Replacement</b>
            <p>
              {(cand.new_route as string) || "—"}
              <br />
              {(cand.new_departure as string) || "—"} · {(cand.carrier as string)}{" "}
              {(cand.flight_number as string)}
              <br />
              {cand.supplier_fare
                ? money(cand.supplier_fare as number, (cand.currency as string) || item.currency)
                : "fare —"}
              <br />
              cabin {(cand.cabin_baggage as string) || "—"} · checked{" "}
              {(cand.checked_baggage as string) || "—"}
              <br />
              {(cand.connection_note as string) || ""}
            </p>
          </div>
        </div>
      )}

      {op.state === "COMPARED" && (
        <ApproveExecute
          op={op}
          kind="recovery"
          idem={idem}
          busy={busy}
          onRun={onRun}
          executeLabel="Execute (record decision — no auto-book)"
          confirmText="Records the approved replacement. It does NOT book a ticket — do that through the normal order flow."
        />
      )}
      {op.state === "APPROVED" && (
        <ApproveExecute
          op={op}
          kind="recovery"
          idem={idem}
          busy={busy}
          onRun={onRun}
          executeLabel="Execute (record decision — no auto-book)"
          confirmText="Records the approved replacement. It does NOT book a ticket — do that through the normal order flow."
        />
      )}
      {op.state === "EXECUTED" && (
        <div className="ops-tops__result is-ok">
          <b>Recovery decision recorded</b>
          <p className="ops-small ops-muted">{(op.result?.note as string) || ""}</p>
        </div>
      )}
    </div>
  );
}

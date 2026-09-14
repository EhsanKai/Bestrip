# V9 Phase 5 — Final Report

Confirmation, Receipt/Invoice & Post-Booking Communication

## Verdict

**V9 PHASE 5 VERDICT: APPROVED**

## Scope of this report

Phase 5 backend work (commits `4d053b7` .. `ca41219`) was implemented and
unit/integration-tested in earlier sessions. What had **not** yet happened
was a fresh, independent adversarial QA pass with a documented verdict, and
a full-regression result recorded after the `ca41219` QA-fix commit. This
report closes that gap.

## What was found in the repository at resume time

- Backend worktree was clean at `HEAD` (`985e210`, which includes `ca41219`
  in its ancestry). No uncommitted Phase 5 backend work existed to reconcile.
- No prior full-suite result or QA report existed on disk after `ca41219`.
- `ca41219` itself documents a real prior QA cycle: Agent 5's independent
  adversarial pass first returned **REJECTED** for two findings (concurrent
  resend race leaking raw SQL errors; `RECOVERY_REQUIRED` readable as
  confirmed), both fixed in that commit.

## Adversarial findings re-investigated this session

### 1. Credit-note full-mirror reconciliation

**Claim:** a FULL credit note that mirrors the original document's line
items must reconcile to the amount actually credited and must not silently
produce inconsistent line-item totals.

**Verdict: false positive — already handled correctly.**
`issue_credit_note` (`src/detoura/services/financial_document_service.py`)
only mirrors the original's line items when `credits_whole_capture` is true
*and* the mirrored components actually sum to the credited amount; otherwise
it falls back to an honest "unknown attribution" credit note (all component
fields `None`), which `FinancialDocument`'s own validators accept. This was
verified empirically, not just read: a receipt issued against a partial
capture (so its line items reconcile to the full *priced* total, not to what
was captured) followed by a full credit note for only what was ever actually
captured correctly produces `has_line_item_breakdown=False` rather than a
mismatched mirror. Added as a permanent regression test:
`test_full_credit_note_drops_mirror_when_it_would_not_reconcile` in
`tests/test_v9_phase5_financial_documents.py`.

### 2. "payment is confirmed" / "(pending confirmation)" strings

**Claim:** these substrings in the `PAYMENT_UNKNOWN` confirmation email read
as asserting payment is confirmed.

**Verdict: false positive — confirmed independently.**
`render_booking_confirmation_email`
(`src/detoura/models/communication.py`) uses them only in forward-looking,
explicitly-qualified context: *"We'll send an update once payment is
confirmed"* (not-yet, future) and *"Quoted Total: ... (pending
confirmation)"* (explicitly marked not final). Neither claims payment has
succeeded now; both are consistent with `PAYMENT UNKNOWN != PAID`. A naive
substring check (e.g. `"payment is confirmed" not in body`) is over-broad
because it matches inside the truthful future-tense sentence. The existing
test suite already scopes this correctly
(`test_payment_unknown_email_avoids_successful` bans `"successful"`, not
`"confirmed"`), so no test or code change was needed.

## Regression

Full suite: **2119 tests collected, all passing** (exit code 0). ~35 skips,
all environmental/opt-in (no Redis reachable locally, live-network image
tests gated behind `DETOURA_RUN_LIVE_IMAGE_TESTS=1`, one search fixture that
produced no itinerary on this run) — none are Phase 5 related and none are
blockers.

Targeted runs, all green:
- `tests/test_v9_phase5_*.py` (financial documents, communication,
  integration, Me/Trips API, Ops confirmations API) — including the new
  credit-note regression test.
- Phase 4 payment domain/service/orchestrator/providers/API security +
  ticket-operations/Ops interaction tests
  (`test_v9_phase4_*.py`, `test_v85_ticket_operations.py`,
  `test_v85_ops*.py`).

## Other release-gate checks

- **Secret scan (manual):** no live-looking keys (`sk_live_`, `whsec_`,
  AWS-style, PEM private keys) in tracked backend source; all `sk_live_`
  hits are in redaction/validation logic and test fixtures using obviously
  fake values. No `.env` files tracked; `.gitignore` covers them. (No
  automated secret-scan tool is part of the current CI gate —
  `.github/workflows/ci.yml` runs backend pytest, frontend lint/build/typecheck,
  and a Docker image build only.)
- **No unrelated scope creep:** the only file changed for this closure is
  the one new regression test plus this report.

## Adversarial coverage already in place (verified via existing green tests)

Confirmation truth, resend concurrency/dedup, UNKNOWN handling, stale-claim
recovery, IDOR-safe ownership reads, and owner-only route checks all have
existing passing tests (e.g. `test_concurrent_resend_via_http_never_leaks_raw_db_error`,
`test_partial_booking_recovery_required_no_false_confirmation`,
`test_ownership_checked_reads_are_idor_safe`,
`test_route_ownership_checks_both_levels`,
`test_unknown_payment_on_a_complete_booking_is_pending_not_confirmed`).

## Blockers

None — no Critical, no High.

## Final Phase 5 checkpoint

This report + the new regression test, committed as the Phase 5 closure
commit on top of `985e210`.

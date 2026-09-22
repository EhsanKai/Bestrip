"""Account deletion + data export (V9 Google auth + account lifecycle §13).

**No legal retention policy is established for this project**, and this
module does not invent one. It implements only the subset that is safely,
unambiguously correct without one:

* **Deletion** deactivates the account, scrubs the one piece of PII the
  account itself carries (its email, to a tombstone value), erases
  credentials/sessions/linked identities, and stops - it never touches
  ``trip_ownership``, ``checkout_snapshots``, ``payment_transactions``,
  ``financial_documents``, or any other booking/financial record, all of
  which key on ``user_id`` (never on email) and may carry immutable
  financial/legal obligations this codebase has no authority to decide are
  safe to erase (§13 "do NOT blindly cascade-delete financial/booking
  records"). A deleted account can never log in again (§ email scrubbed,
  status DELETED) and can never be recreated with the same email by
  coincidence (the tombstone is derived from ``user_id``, not reused).
  As of the Limited Beta privacy policy slice, deletion additionally
  anonymizes ``recipient_address`` on this account's
  ``customer_communications`` rows (see
  ``persistence/communications.py::scrub_recipient_for_user``) - the one
  communications field Product/Legal have now closed as having no
  retained purpose. ``bookings.lead_email``/``lead_name`` are booking
  truth, not a communication record, and remain untouched by this or any
  other deletion step, unchanged from before.
* **Export** returns exactly the account-owned metadata this codebase can
  state with confidence belongs to the account: identity fields, linked
  Google identities (provider + email + timestamps, never the internal
  ``provider_subject``, which is an integration detail, not a fact about
  the user's own data), and the list of booking ids they own. A full
  export of booking/financial/communication history is explicitly NOT
  built here - see the report's classification - because deciding what of
  that is "this person's data" versus "Detoura's own transaction record"
  is a product/legal scoping question, not one this slice can answer
  safely by guessing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.account import AccountStatus
from ..persistence import accounts as store
from ..persistence import audit
from ..persistence import communications as communications_store
from ..persistence.db import Database


class AccountLifecycleError(Exception):
    """A user-facing failure."""


def export_account_data(db: Database, *, user_id: str) -> dict:
    user = store.get_user(db, user_id)
    if user is None or user["status"] == AccountStatus.DELETED.value:
        raise AccountLifecycleError("This account is not available.")

    identities = [
        {
            "provider": row["provider"],
            "provider_email": row["provider_email"],
            "linked_at": row["created_at"],
        }
        for row in store.list_identities_for_user(db, user_id)
    ]
    return {
        "account": {
            "user_id": user["user_id"],
            "email": user["email_normalized"],
            "status": user["status"],
            "has_password": user["password_hash"] is not None,
            "created_at": user["created_at"],
            "last_login_at": user["last_login_at"],
        },
        "linked_identities": identities,
        "owned_booking_ids": list(store.list_trip_ids_for_user(db, user_id)),
    }


def delete_account(db: Database, *, user_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    user = store.get_user(db, user_id)
    if user is None or user["status"] == AccountStatus.DELETED.value:
        raise AccountLifecycleError("This account is not available.")

    store.scrub_account_for_deletion(db, user_id, now=now)
    communications_store.scrub_recipient_for_user(db, user_id, now=now)
    audit.record(db, actor=user_id, action="account_deleted", target_type="user_account", target_id=user_id)

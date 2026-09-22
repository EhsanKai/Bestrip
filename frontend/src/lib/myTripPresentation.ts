import type { FinancialDocument, MyTripSummary, TripConfirmation } from "../api/types";

type ConsumerTone = "good" | "attention" | "pending" | "neutral" | "failed";
export interface ConsumerState {
  key:
    | "READY_TO_PAY"
    | "PAYMENT_PROCESSING"
    | "PAYMENT_UNKNOWN"
    | "READY_TO_CONFIRM"
    | "BOOKING_IN_PROGRESS"
    | "PRICE_CHANGED"
    | "CONFIRMED"
    | "RECOVERY_REQUIRED"
    | "FAILED";
  label: string;
  detail: string;
  tone: ConsumerTone;
  actionRequired: boolean;
}
export function stateFromTrip(trip: MyTripSummary, confirmation: TripConfirmation | null): ConsumerState {
  const confirmationStatus = confirmation?.status;
  if (confirmationStatus === "CONFIRMED") {
    return consumerState("CONFIRMED", "Confirmed", "Detoura has confirmed this journey from booking and payment truth.", "good");
  }
  if (confirmationStatus === "PARTIAL_RECOVERY") return recoveryState("Some of this journey or its payment state needs Detoura review.");
  if (confirmationStatus === "PENDING_VERIFICATION") {
    return consumerState("PAYMENT_UNKNOWN", "Verification in progress", "The final outcome is not proven yet. This is not paid, failed, or confirmed.", "pending");
  }
  if (confirmationStatus === "CANCELLED" || confirmationStatus === "SUPERSEDED") {
    return consumerState("FAILED", humanize(confirmationStatus), "This confirmation is no longer active.", "failed");
  }

  switch (trip.phase) {
    case "awaiting_travelers":
      return consumerState("READY_TO_PAY", "Traveler details needed", "The backend has not received the traveler party for this booking.", "neutral", true);
    case "awaiting_confirmation":
      return consumerState("READY_TO_CONFIRM", "Awaiting confirmation", "The trip is priced, but backend truth does not say it is booked.", "neutral", true);
    case "revalidating":
    case "issuing":
      return consumerState("BOOKING_IN_PROGRESS", "Booking in progress", "Detoura is still working. Worker start is not shown as confirmed.", "pending");
    case "reconfirm_required":
    case "price_inconsistent":
      return consumerState("PRICE_CHANGED", "Price needs review", "The journey cannot continue on stale pricing.", "attention", true);
    case "partial_failure":
      return recoveryState("Part of the journey failed or needs human recovery.");
    case "guided_booking":
      return consumerState("BOOKING_IN_PROGRESS", "Self-service booking", "Detoura is guiding the journey, but it has not proven provider confirmation.", "pending");
    case "complete":
      return consumerState("PAYMENT_PROCESSING", "Awaiting final proof", "The booking phase is complete, but no consumer confirmation is exposed yet.", "pending");
    case "failed":
      return consumerState("FAILED", "Not booked", "The backend says this booking failed.", "failed");
    default:
      return consumerState("PAYMENT_UNKNOWN", "Status being checked", "This state is not confirmed until backend confirmation says so.", "pending");
  }
}

function recoveryState(detail: string): ConsumerState {
  return consumerState("RECOVERY_REQUIRED", "Recovery required", detail, "attention", true);
}

function consumerState(
  key: ConsumerState["key"],
  label: string,
  detail: string,
  tone: ConsumerTone,
  actionRequired = false,
): ConsumerState {
  return { key, label, detail, tone, actionRequired };
}

export function routeText(trip: MyTripSummary): string {
  return trip.route_cities.length ? trip.route_cities.join(" -> ") : trip.trip_label || "Detoura trip";
}

export function documentLabel(document: FinancialDocument): string {
  return `${humanize(document.document_type)} ${document.document_number}`;
}

export function documentTypeForAnalytics(value: string): "receipt" | "invoice" | "credit_note" | "unknown" {
  const normalized = value.toLowerCase();
  if (normalized.includes("receipt")) return "receipt";
  if (normalized.includes("invoice")) return "invoice";
  if (normalized.includes("credit")) return "credit_note";
  return "unknown";
}

export function humanize(value: string): string {
  return value
    .toLowerCase()
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

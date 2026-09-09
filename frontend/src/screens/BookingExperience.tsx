import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { DetouraApiError } from "../api/types";
import type {
  BookingIntent,
  TravelPass as TravelPassData,
  TravelerInput,
  TripRecommendation,
} from "../api/types";
import { Button } from "../components/ui/Button";
import { JourneyPoster } from "../components/trip/JourneyPoster";
import { TravelPass } from "../components/booking/TravelPass";
import { money, signedMoney, clockTime, dayMonth } from "../lib/format";
import "./BookingExperience.css";

type Phase =
  | "traveler"
  | "review"
  | "working" // revalidating / issuing — driven by the polled intent
  | "reconfirm"
  | "pass";

interface TravelerDraft {
  given_name: string;
  family_name: string;
  born_on: string;
  email: string;
  phone: string;
}

const EMPTY: TravelerDraft = {
  given_name: "",
  family_name: "",
  born_on: "",
  email: "",
  phone: "",
};

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE_RE = /^\+?[0-9 .\-()]{6,20}$/;

function draftErrors(d: TravelerDraft): Partial<Record<keyof TravelerDraft, string>> {
  const e: Partial<Record<keyof TravelerDraft, string>> = {};
  if (d.given_name.trim().length < 1) e.given_name = "Required";
  if (d.family_name.trim().length < 1) e.family_name = "Required";
  if (!d.born_on) e.born_on = "Required";
  else {
    const y = new Date(d.born_on).getFullYear();
    if (y < 1900 || y > new Date().getFullYear()) e.born_on = "Check this date";
  }
  if (!EMAIL_RE.test(d.email.trim())) e.email = "Enter a valid email";
  if (!PHONE_RE.test(d.phone.trim())) e.phone = "Enter a valid phone number";
  return e;
}

const STEP_LABELS = ["Traveller", "Review", "Ticketing", "Pass"];

function stepIndex(phase: Phase): number {
  if (phase === "traveler") return 0;
  if (phase === "review") return 1;
  if (phase === "pass") return 3;
  return 2;
}

export function BookingExperience({
  trip,
  onBack,
  onViewDetails,
}: {
  trip: TripRecommendation;
  onBack: () => void;
  onViewDetails: () => void;
}) {
  const partySize = 1; // the pass shows one lead traveller; multi-pax uses the same form repeated
  const [phase, setPhase] = useState<Phase>("traveler");
  const [drafts, setDrafts] = useState<TravelerDraft[]>(() =>
    Array.from({ length: partySize }, () => ({ ...EMPTY })),
  );
  const [showErrors, setShowErrors] = useState(false);
  const [bookingId, setBookingId] = useState<string | null>(null);
  const [intent, setIntent] = useState<BookingIntent | null>(null);
  const [pass, setPass] = useState<TravelPassData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  const legs = trip.legs;

  // --- create the booking intent (DEMO_ONLY from the selected trip) --------
  const ensureIntent = useCallback(async (): Promise<string> => {
    if (bookingId) return bookingId;
    const created = await api.createBookingIntent({
      demo_trip_label: trip.route,
      demo_currency: trip.currency,
      demo_total: trip.total_price,
      demo_travelers: partySize,
      demo_legs: legs.map((l) => {
        const [carrier, ...rest] = (l.operator || "").split(" ");
        return {
          origin: l.from,
          destination: l.to,
          departure: l.departure,
          arrival: l.arrival,
          carrier: carrier || "",
          flight_number: rest.join(" "),
          price_per_person: l.price_per_person,
        };
      }),
    });
    setBookingId(created.booking_id);
    setIntent(created);
    return created.booking_id;
  }, [bookingId, legs, trip, partySize]);

  // --- polling while the run is in flight --------------------------------
  useEffect(() => {
    if (phase !== "working" || !bookingId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await api.getBookingIntent(bookingId);
        if (cancelled) return;
        setIntent(next);
        if (next.phase === "reconfirm_required") {
          setPhase("reconfirm");
          return;
        }
        if (next.pass_available) {
          const tp = await api.getTravelPass(bookingId);
          if (cancelled) return;
          setPass(tp);
          setPhase("pass");
          return;
        }
        pollRef.current = window.setTimeout(tick, 450);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof DetouraApiError ? e.message : "Something went wrong.");
      }
    };
    pollRef.current = window.setTimeout(tick, 250);
    return () => {
      cancelled = true;
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, [phase, bookingId]);

  const submitTravelers = async () => {
    setShowErrors(true);
    if (drafts.some((d) => Object.keys(draftErrors(d)).length > 0)) return;
    setBusy(true);
    setError(null);
    try {
      const id = await ensureIntent();
      const travelers: TravelerInput[] = drafts.map((d) => ({
        given_name: d.given_name.trim(),
        family_name: d.family_name.trim(),
        born_on: d.born_on,
        email: d.email.trim(),
        phone: d.phone.trim(),
      }));
      const next = await api.submitTravelers(id, travelers);
      setIntent(next);
      setPhase("review");
    } catch (e) {
      setError(e instanceof DetouraApiError ? e.message : "Could not save traveller details.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (toleranceAbsolute = 25) => {
    if (!bookingId) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.confirmBooking(bookingId, { tolerance_absolute: toleranceAbsolute });
      setIntent(next);
      setPhase("working");
    } catch (e) {
      setError(e instanceof DetouraApiError ? e.message : "Could not confirm the journey.");
    } finally {
      setBusy(false);
    }
  };

  const step = stepIndex(phase);

  return (
    <div className="booking">
      <div className="container booking__wrap">
        <button className="booking__back" onClick={onBack}>
          ← Back to journey
        </button>

        <header className="booking__head">
          <div>
            <span className="eyebrow">ONE JOURNEY · ONE CONFIRMATION</span>
            <h1>We’ll handle the tickets.</h1>
            <p>
              Enter your details once. Detoura coordinates every flight behind
              this journey — and this test version never asks for payment.
            </p>
          </div>
          <div className="booking__poster">
            <JourneyPoster cities={trip.cities} rank={trip.rank} />
          </div>
        </header>

        <div className="booking__steps">
          {STEP_LABELS.map((label, i) => (
            <div className={i <= step ? "is-on" : ""} key={label}>
              <b>{i < step ? "✓" : i + 1}</b>
              <span>{label}</span>
            </div>
          ))}
        </div>

        <section className="booking__panel">
          {error && <div className="booking__error">{error}</div>}

          {phase === "traveler" && (
            <TravelerStep
              drafts={drafts}
              setDrafts={setDrafts}
              showErrors={showErrors}
              busy={busy}
              onContinue={submitTravelers}
            />
          )}

          {phase === "review" && intent && (
            <ReviewStep
              intent={intent}
              legs={legs}
              busy={busy}
              onBack={() => setPhase("traveler")}
              onConfirm={() => confirm(25)}
            />
          )}

          {phase === "working" && intent && <WorkingStep intent={intent} />}

          {phase === "reconfirm" && intent && (
            <ReconfirmStep
              intent={intent}
              busy={busy}
              onReconfirm={() => confirm(1_000_000)}
              onAbandon={onBack}
            />
          )}

          {phase === "pass" && pass && (
            <div className="booking__passwrap">
              <TravelPass pass={pass} />
              <div className="booking__actions">
                <Button variant="secondary" onClick={onViewDetails}>
                  View journey details
                </Button>
                <Button onClick={onBack}>Back to trips</Button>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function TravelerStep({
  drafts,
  setDrafts,
  showErrors,
  busy,
  onContinue,
}: {
  drafts: TravelerDraft[];
  setDrafts: (d: TravelerDraft[]) => void;
  showErrors: boolean;
  busy: boolean;
  onContinue: () => void;
}) {
  const update = (i: number, field: keyof TravelerDraft, value: string) => {
    const next = drafts.map((d, idx) => (idx === i ? { ...d, [field]: value } : d));
    setDrafts(next);
  };

  return (
    <>
      <span className="eyebrow">Traveller details</span>
      <h2>Who’s taking this journey?</h2>
      <p className="muted">
        Entered once and used server-side to prepare the selected tickets. Your
        details are not stored in this browser and never appear in a link.
      </p>

      {drafts.map((d, i) => {
        const errs = showErrors ? draftErrors(d) : {};
        return (
          <div className="booking__grid" key={i}>
            <label>
              First name
              <input
                value={d.given_name}
                onChange={(e) => update(i, "given_name", e.target.value)}
                autoComplete="given-name"
              />
              {errs.given_name && <em>{errs.given_name}</em>}
            </label>
            <label>
              Last name
              <input
                value={d.family_name}
                onChange={(e) => update(i, "family_name", e.target.value)}
                autoComplete="family-name"
              />
              {errs.family_name && <em>{errs.family_name}</em>}
            </label>
            <label>
              Date of birth
              <input
                type="date"
                value={d.born_on}
                onChange={(e) => update(i, "born_on", e.target.value)}
              />
              {errs.born_on && <em>{errs.born_on}</em>}
            </label>
            <label>
              Email
              <input
                type="email"
                value={d.email}
                onChange={(e) => update(i, "email", e.target.value)}
                autoComplete="email"
              />
              {errs.email && <em>{errs.email}</em>}
            </label>
            <label>
              Phone
              <input
                type="tel"
                value={d.phone}
                onChange={(e) => update(i, "phone", e.target.value)}
                autoComplete="tel"
                placeholder="+49 170 1234567"
              />
              {errs.phone && <em>{errs.phone}</em>}
            </label>
          </div>
        );
      })}

      <div className="booking__test">
        Payment is not required in this test version.
      </div>

      <div className="booking__actions">
        <Button size="lg" onClick={onContinue} disabled={busy}>
          {busy ? "Saving…" : "Continue to review"}
        </Button>
      </div>
    </>
  );
}

function ReviewStep({
  intent,
  legs,
  busy,
  onBack,
  onConfirm,
}: {
  intent: BookingIntent;
  legs: TripRecommendation["legs"];
  busy: boolean;
  onBack: () => void;
  onConfirm: () => void;
}) {
  const unknowns = intent.items.filter((i) => i.checked_baggage === "unknown");
  return (
    <>
      <span className="eyebrow">Review your journey</span>
      <h2>
        One journey. {intent.items.length} ticket
        {intent.items.length === 1 ? "" : "s"}.
      </h2>

      <div className="booking__route">
        {intent.route_cities.map((city, i) => (
          <span key={i}>
            {city}
            {i < intent.route_cities.length - 1 && <i>↓</i>}
          </span>
        ))}
      </div>

      <div className="booking__tickets">
        {intent.items.map((it, i) => {
          const leg = legs[i];
          return (
            <div className="booking__ticket" key={it.sequence}>
              <header>
                <b>Ticket {it.sequence}</b>
                <strong>
                  {it.origin_airport} → {it.destination_airport}
                </strong>
              </header>
              <dl>
                <div>
                  <dt>Date</dt>
                  <dd>{dayMonth(it.departure)}</dd>
                </div>
                <div>
                  <dt>Departs</dt>
                  <dd>{clockTime(it.departure)}</dd>
                </div>
                <div>
                  <dt>Arrives</dt>
                  <dd>{clockTime(it.arrival)}</dd>
                </div>
                <div>
                  <dt>Carrier</dt>
                  <dd>{it.carrier || leg?.operator || "—"}</dd>
                </div>
                <div>
                  <dt>Cabin bag</dt>
                  <dd>{it.cabin_baggage}</dd>
                </div>
                <div>
                  <dt>Checked bag</dt>
                  <dd>{it.checked_baggage}</dd>
                </div>
                <div>
                  <dt>Fare</dt>
                  <dd>{money(it.price_per_person, it.currency)}</dd>
                </div>
              </dl>
            </div>
          );
        })}
      </div>

      <div className="booking__summary">
        <div>
          <span>Trip total</span>
          <strong>{money(intent.discovered_total, intent.currency)}</strong>
        </div>
        <div>
          <span>Travellers</span>
          <strong>{intent.party_size}</strong>
        </div>
        {unknowns.length > 0 && (
          <div className="booking__summary-note">
            {unknowns.length} leg{unknowns.length === 1 ? "" : "s"} with unknown
            checked-baggage terms — Detoura will not claim a price it cannot stand behind.
          </div>
        )}
      </div>

      <div className="booking__pay">
        <b>Payment</b>
        <span>Payment is not required in this test version.</span>
      </div>

      <div className="booking__actions">
        <Button variant="secondary" onClick={onBack} disabled={busy}>
          Back
        </Button>
        <Button size="lg" onClick={onConfirm} disabled={busy}>
          {busy ? "Confirming…" : "Confirm journey"}
        </Button>
      </div>
    </>
  );
}

const REVALIDATION_STEPS = [
  "Checking current availability",
  "Checking latest fares",
  "Checking baggage",
  "Preparing tickets",
];

function WorkingStep({ intent }: { intent: BookingIntent }) {
  const revalidating = intent.phase === "revalidating";

  return (
    <>
      <span className="eyebrow">
        {revalidating ? "Checking your trip" : "Preparing your journey"}
      </span>
      <h2>
        {revalidating
          ? "Checking your trip before ticketing…"
          : "Your journey is being prepared."}
      </h2>

      {revalidating ? (
        <ul className="booking__checklist">
          {REVALIDATION_STEPS.map((label) => (
            <li key={label}>
              <span className="booking__spin" aria-hidden /> {label}
            </li>
          ))}
        </ul>
      ) : (
        <div className="booking__progress">
          {intent.items.map((it) => {
            const done = it.state === "CONFIRMED";
            const failed =
              it.state === "FAILED" ||
              it.state === "UNAVAILABLE" ||
              it.state === "EXPIRED";
            const active = it.state === "BOOKING" || it.state === "USER_CONFIRMED";
            const cls = done ? "done" : failed ? "failed" : active ? "active" : "";
            return (
              <div key={it.sequence} className={cls}>
                <i>{done ? "✓" : failed ? "✕" : active ? "●" : it.sequence}</i>
                <span>
                  <strong>
                    {it.origin_city} → {it.destination_city}
                  </strong>
                  <small>
                    {done
                      ? "Ticket secured"
                      : failed
                        ? it.detail || "Could not be issued"
                        : active
                          ? "Issuing ticket…"
                          : "Waiting"}
                  </small>
                </span>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

function ReconfirmStep({
  intent,
  busy,
  onReconfirm,
  onAbandon,
}: {
  intent: BookingIntent;
  busy: boolean;
  onReconfirm: () => void;
  onAbandon: () => void;
}) {
  const before = intent.discovered_total;
  const after = intent.current_total ?? before;
  const delta = Math.round((after - before) * 100) / 100;
  return (
    <>
      <span className="eyebrow">Something changed</span>
      <h2>The price moved before we could ticket.</h2>
      <p className="muted">{intent.reconfirm_note}</p>

      <div className="booking__delta">
        <div>
          <span>Price when selected</span>
          <strong>{money(before, intent.currency)}</strong>
        </div>
        <div>
          <span>Current price</span>
          <strong>{money(after, intent.currency)}</strong>
        </div>
        <div className={delta > 0 ? "up" : "down"}>
          <span>Difference</span>
          <strong>{signedMoney(delta, intent.currency)}</strong>
        </div>
      </div>

      <p className="muted">
        Nothing has been booked. Confirm again to proceed at the current price,
        or go back.
      </p>

      <div className="booking__actions">
        <Button variant="secondary" onClick={onAbandon} disabled={busy}>
          Don’t book
        </Button>
        <Button size="lg" onClick={onReconfirm} disabled={busy}>
          {busy ? "Confirming…" : `Confirm at ${money(after, intent.currency)}`}
        </Button>
      </div>
    </>
  );
}

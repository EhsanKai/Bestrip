import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { DetouraApiError } from "../api/types";
import type {
  BookingIntent,
  CommercialSummary,
  SelfServiceItinerary,
  SelfServiceTicket,
  ServiceTier,
  ServiceTierOption,
  SetCommercialOptionsRequest,
  TravelPass as TravelPassData,
  TravelerInput,
  TripRecommendation,
} from "../api/types";
import { Button } from "../components/ui/Button";
import { JourneyPoster } from "../components/trip/JourneyPoster";
import { TravelPass } from "../components/booking/TravelPass";
import { money, signedMoney, clockTime, dayMonth } from "../lib/format";
import { funnel } from "../lib/funnel";
import "./BookingExperience.css";

/**
 * [airline badge] Airline Name · Flight Number, with a graceful fallback to
 * [generic badge] Carrier Code · Flight Number when we don't have the name.
 * A missing logo/name never hides the ticket. No airline names or logos are
 * hardcoded here — the server resolves them from centralized AirlineMetadata.
 */
function AirlineTag({
  code,
  name,
  flightNumber,
  operatingCode,
  operatingName,
}: {
  code?: string;
  name?: string;
  flightNumber?: string;
  operatingCode?: string;
  operatingName?: string;
}) {
  const label = name || code || "—";
  const known = Boolean(name && name !== code);
  return (
    <span className="airline-tag">
      <span
        className={`airline-tag__badge${known ? "" : " airline-tag__badge--generic"}`}
        aria-hidden="true"
      >
        {(code || "?").slice(0, 2)}
      </span>
      <span>
        {label}
        {flightNumber ? ` · ${code || ""}${flightNumber}` : ""}
        {operatingCode && operatingCode !== code ? (
          <span className="airline-tag__op">
            {" "}
            operated by {operatingName || operatingCode}
          </span>
        ) : null}
      </span>
    </span>
  );
}

type Phase =
  | "tier"
  | "traveler"
  | "review"
  | "working"
  | "reconfirm"
  | "pass"
  | "guided";

type Flow = "self_service" | "managed";

interface TravelerDraft {
  given_name: string;
  family_name: string;
  born_on: string;
  email: string;
  phone: string;
  nationality: string;
  passport_number: string;
  passport_issuing_country: string;
  passport_expiry: string;
}

const EMPTY: TravelerDraft = {
  given_name: "",
  family_name: "",
  born_on: "",
  email: "",
  phone: "",
  nationality: "",
  passport_number: "",
  passport_issuing_country: "",
  passport_expiry: "",
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
  // Passport is optional in this test flow (nothing is issued); if a number is
  // given, an expiry must come with it.
  if (d.passport_number.trim() && !d.passport_expiry)
    e.passport_expiry = "Add the expiry date";
  return e;
}

/** Itemised money always shows exact cents so the lines reconcile. */
function money2(amount: number, currency: string): string {
  const symbol = currency === "EUR" ? "€" : `${currency} `;
  return `${symbol}${amount.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function stepLabels(flow: Flow): string[] {
  return flow === "self_service"
    ? ["Service", "Traveller", "Review", "Book tickets"]
    : ["Service", "Traveller", "Review", "Ticketing", "Pass"];
}

function stepIndex(phase: Phase, flow: Flow): number {
  if (phase === "tier") return 0;
  if (phase === "traveler") return 1;
  if (phase === "review") return 2;
  if (phase === "guided" || phase === "pass") return flow === "self_service" ? 3 : 4;
  return 3; // working / reconfirm
}

export function BookingExperience({
  trip,
  travelers = 1,
  onBack,
  onViewDetails,
}: {
  trip: TripRecommendation;
  travelers?: number;
  onBack: () => void;
  onViewDetails: () => void;
}) {
  // The journey's authoritative traveller count — one form per traveller, one
  // TravelerParty for the journey. Never derived from anything editable here.
  const partySize = Math.max(1, Math.round(travelers));
  const [phase, setPhase] = useState<Phase>("tier");
  const [drafts, setDrafts] = useState<TravelerDraft[]>(() =>
    Array.from({ length: partySize }, () => ({ ...EMPTY })),
  );
  const [showErrors, setShowErrors] = useState(false);
  const [bookingId, setBookingId] = useState<string | null>(null);
  const [intent, setIntent] = useState<BookingIntent | null>(null);
  const [pass, setPass] = useState<TravelPassData | null>(null);
  const [itinerary, setItinerary] = useState<SelfServiceItinerary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Basic is the default and never a pre-selected paid upgrade.
  const [tier, setTier] = useState<ServiceTier>("BASIC");
  const [promoInput, setPromoInput] = useState("");
  const pollRef = useRef<number | null>(null);

  const legs = trip.legs;
  const flow: Flow = tier === "BASIC" ? "self_service" : "managed";

  const ensureIntent = useCallback(
    async (chosenTier: ServiceTier): Promise<string> => {
      if (bookingId) return bookingId;
      const created = await api.createBookingIntent({
        demo_trip_label: trip.route,
        demo_currency: trip.currency,
        demo_travelers: partySize,
        // The whole-trip estimate — for display only. The server prices the
        // supplier transport from the legs, never from this.
        demo_trip_estimate: {
          total: trip.total_price,
          transport: trip.costs.transport,
          accommodation: trip.costs.accommodation,
          transfer: trip.costs.ground_transfer,
        },
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
        service_tier: chosenTier,
      });
      setBookingId(created.booking_id);
      setIntent(created);
      return created.booking_id;
    },
    [bookingId, legs, trip, partySize],
  );

  // Create the intent as soon as the screen opens so the tier comparison shows
  // real prices from the start.
  useEffect(() => {
    void ensureIntent("BASIC").catch(() =>
      setError("Could not start a booking. Go back and try again."),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const changeCommercial = useCallback(
    async (body: SetCommercialOptionsRequest) => {
      if (!bookingId) return;
      setBusy(true);
      setError(null);
      try {
        const next = await api.setCommercialOptions(bookingId, body);
        setIntent(next);
        if (next.commercial) setTier(next.commercial.service_tier);
        if (body.promo_code && next.commercial?.promo_accepted) {
          funnel("PROMO_APPLIED", {
            tier: next.commercial.service_tier,
            props: { promo_code: next.commercial.promo_code ?? "" },
          });
        }
      } catch (e) {
        setError(
          e instanceof DetouraApiError ? e.message : "Could not update the price.",
        );
      } finally {
        setBusy(false);
      }
    },
    [bookingId],
  );

  const chooseTier = async (next: ServiceTier) => {
    if (next !== tier) funnel("TIER_SWITCHED", { tier: next });
    setTier(next);
    if (bookingId) await changeCommercial({ service_tier: next });
  };

  // Poll only the managed (All-in-One) run.
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
          funnel(tp.status === "ready" ? "BOOKED" : "FAILED", {
            tier,
            props: { outcome: tp.status },
          });
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
  }, [phase, bookingId, tier]);

  const submitTravelers = async () => {
    setShowErrors(true);
    if (drafts.some((d) => Object.keys(draftErrors(d)).length > 0)) return;
    setBusy(true);
    setError(null);
    try {
      const id = await ensureIntent(tier);
      const travelers: TravelerInput[] = drafts.map((d) => ({
        given_name: d.given_name.trim(),
        family_name: d.family_name.trim(),
        born_on: d.born_on,
        email: d.email.trim(),
        phone: d.phone.trim(),
        nationality: d.nationality.trim().toUpperCase() || null,
        passport_number: d.passport_number.trim() || null,
        passport_issuing_country:
          d.passport_issuing_country.trim().toUpperCase() || null,
        passport_expiry: d.passport_expiry || null,
        document_type: d.passport_number.trim() ? "passport" : null,
      }));
      const next = await api.submitTravelers(id, travelers);
      setIntent(next);
      funnel("REVIEW", { tier });
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
    funnel("CONFIRM", { tier });
    try {
      const next = await api.confirmBooking(bookingId, { tolerance_absolute: toleranceAbsolute });
      setIntent(next);
      if (next.service_flow === "self_service") {
        const it = await api.getItinerary(bookingId);
        setItinerary(it);
        funnel("BOOKED", { tier, props: { outcome: "itinerary_ready" } });
        setPhase("guided");
      } else {
        setPhase("working");
      }
    } catch (e) {
      funnel("FAILED", { tier });
      setError(e instanceof DetouraApiError ? e.message : "Could not confirm the journey.");
    } finally {
      setBusy(false);
    }
  };

  const guidedAction = async (
    run: () => Promise<SelfServiceItinerary>,
  ) => {
    setBusy(true);
    setError(null);
    try {
      setItinerary(await run());
    } catch (e) {
      setError(e instanceof DetouraApiError ? e.message : "Could not update the ticket.");
    } finally {
      setBusy(false);
    }
  };

  const labels = stepLabels(flow);
  const step = stepIndex(phase, flow);

  return (
    <div className="booking">
      <div className="container booking__wrap">
        <button className="booking__back" onClick={onBack}>
          ← Back to journey
        </button>

        <header className="booking__head">
          <div>
            <span className="eyebrow">ONE JOURNEY · ONE SET OF DETAILS</span>
            <h1>We’ll help you book it.</h1>
            <p>
              Enter your details once for the whole journey. Choose whether
              Detoura books the tickets for you or guides you through it — this
              test version never asks for payment.
            </p>
          </div>
          <div className="booking__poster">
            <JourneyPoster cities={trip.cities} rank={trip.rank} />
          </div>
        </header>

        <div className="booking__steps">
          {labels.map((label, i) => (
            <div className={i <= step ? "is-on" : ""} key={label}>
              <b>{i < step ? "✓" : i + 1}</b>
              <span>{label}</span>
            </div>
          ))}
        </div>

        <section className="booking__panel">
          {error && <div className="booking__error">{error}</div>}

          {phase === "tier" && (
            <TierStep
              options={intent?.commercial?.tier_options ?? []}
              selected={tier}
              currency={trip.currency}
              busy={busy}
              onSelect={chooseTier}
              onContinue={() => {
                funnel("TIER_SELECTED", { tier });
                setPhase("traveler");
              }}
            />
          )}

          {phase === "traveler" && (
            <TravelerStep
              drafts={drafts}
              setDrafts={setDrafts}
              showErrors={showErrors}
              busy={busy}
              flow={flow}
              onBack={() => setPhase("tier")}
              onContinue={submitTravelers}
            />
          )}

          {phase === "review" && intent && (
            <ReviewStep
              intent={intent}
              legs={legs}
              busy={busy}
              flow={flow}
              promoInput={promoInput}
              setPromoInput={setPromoInput}
              onCommercialChange={changeCommercial}
              onChangeService={() => setPhase("tier")}
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

          {phase === "guided" && itinerary && (
            <GuidedStep
              itinerary={itinerary}
              busy={busy}
              onStart={(seq) =>
                guidedAction(() => api.startTicketBooking(itinerary.booking_id, seq))
              }
              onMark={(seq, state, reference) =>
                guidedAction(() =>
                  api.markTicket(itinerary.booking_id, seq, { state, reference }),
                )
              }
              onDone={onBack}
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

function TierStep({
  options,
  selected,
  currency,
  busy,
  onSelect,
  onContinue,
}: {
  options: ServiceTierOption[];
  selected: ServiceTier;
  currency: string;
  busy: boolean;
  onSelect: (t: ServiceTier) => void;
  onContinue: () => void;
}) {
  const ordered = [...options].sort((a) =>
    a.tier === "BASIC" ? -1 : 1,
  );
  return (
    <>
      <span className="eyebrow">Your service</span>
      <h2>How would you like to book?</h2>
      <p className="muted">
        Same journey, same fares either way. The difference is who books and
        manages the tickets.
      </p>

      <div className="booking__tiers booking__tiers--compare">
        {ordered.map((opt) => (
          <button
            type="button"
            key={opt.tier}
            className={`booking__tier${opt.tier === selected ? " is-on" : ""}`}
            aria-pressed={opt.tier === selected}
            disabled={busy}
            onClick={() => onSelect(opt.tier)}
          >
            <span className="booking__tier-top">
              <b>{opt.label}</b>
              <span className="booking__tier-tagline">{opt.tagline}</span>
              {opt.recommended && (
                <span className="booking__tier-badge">Recommended</span>
              )}
            </span>
            <span className="booking__tier-summary">{opt.summary}</span>
            <span className="booking__tier-price numeric">
              {opt.customer_total > 0
                ? money2(opt.customer_total, currency)
                : "—"}
              <small> total</small>
            </span>
            <span className="booking__tier-fee subtle">
              includes Detoura {opt.label} fee {money2(opt.detoura_fee, currency)}
            </span>
            <ul className="booking__tier-feats">
              {opt.included.map((f) => (
                <li key={f} className="yes">
                  {f}
                </li>
              ))}
              {opt.not_included.map((f) => (
                <li key={f} className="no">
                  {f}
                </li>
              ))}
            </ul>
          </button>
        ))}
      </div>

      <div className="booking__actions">
        <Button size="lg" onClick={onContinue} disabled={busy || options.length === 0}>
          Continue with {selected === "BASIC" ? "Basic" : "All-in-One"}
        </Button>
      </div>
    </>
  );
}

function TravelerStep({
  drafts,
  setDrafts,
  showErrors,
  busy,
  flow,
  onBack,
  onContinue,
}: {
  drafts: TravelerDraft[];
  setDrafts: (d: TravelerDraft[]) => void;
  showErrors: boolean;
  busy: boolean;
  flow: Flow;
  onBack: () => void;
  onContinue: () => void;
}) {
  const update = (i: number, field: keyof TravelerDraft, value: string) => {
    const next = drafts.map((d, idx) => (idx === i ? { ...d, [field]: value } : d));
    setDrafts(next);
  };

  const many = drafts.length > 1;
  return (
    <>
      <span className="eyebrow">Traveller details</span>
      <h2>
        {many
          ? `Who’s taking this journey? (${drafts.length} travellers)`
          : "Who’s taking this journey?"}
      </h2>
      <p className="muted">
        Enter each traveller <b>once</b> — the details are reused for every
        ticket on the journey. They’re not stored in this browser and never
        appear in a link.
        {flow === "self_service" &&
          " An airline site may still ask you to enter them there; that's its requirement, not Detoura's."}
      </p>

      {drafts.map((d, i) => {
        const errs = showErrors ? draftErrors(d) : {};
        return (
          <div className="booking__traveller" key={i}>
            {many && (
              <h3 className="booking__traveller-h">Traveller {i + 1} of {drafts.length}</h3>
            )}
            <div className="booking__grid">
              <label>
                First name
                <input
                  value={d.given_name}
                  onChange={(e) => update(i, "given_name", e.target.value)}
                  autoComplete={i === 0 ? "given-name" : "off"}
                />
                {errs.given_name && <em>{errs.given_name}</em>}
              </label>
              <label>
                Last name
                <input
                  value={d.family_name}
                  onChange={(e) => update(i, "family_name", e.target.value)}
                  autoComplete={i === 0 ? "family-name" : "off"}
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
                  autoComplete={i === 0 ? "email" : "off"}
                />
                {errs.email && <em>{errs.email}</em>}
              </label>
              <label>
                Phone
                <input
                  type="tel"
                  value={d.phone}
                  onChange={(e) => update(i, "phone", e.target.value)}
                  autoComplete={i === 0 ? "tel" : "off"}
                  placeholder="+49 170 1234567"
                />
                {errs.phone && <em>{errs.phone}</em>}
              </label>
            </div>
            <details className="booking__doc">
              <summary>
                Travel document
                <span className="subtle"> — optional for this test; required for international ticketing</span>
              </summary>
              <div className="booking__grid">
                <label>
                  Nationality (2-letter)
                  <input
                    value={d.nationality}
                    maxLength={2}
                    placeholder="DE"
                    onChange={(e) => update(i, "nationality", e.target.value)}
                  />
                </label>
                <label>
                  Passport / document number
                  <input
                    value={d.passport_number}
                    onChange={(e) => update(i, "passport_number", e.target.value)}
                    autoComplete="off"
                  />
                </label>
                <label>
                  Issuing country (2-letter)
                  <input
                    value={d.passport_issuing_country}
                    maxLength={2}
                    placeholder="DE"
                    onChange={(e) =>
                      update(i, "passport_issuing_country", e.target.value)
                    }
                  />
                </label>
                <label>
                  Expiry date
                  <input
                    type="date"
                    value={d.passport_expiry}
                    onChange={(e) => update(i, "passport_expiry", e.target.value)}
                  />
                  {errs.passport_expiry && <em>{errs.passport_expiry}</em>}
                </label>
              </div>
            </details>
          </div>
        );
      })}

      <div className="booking__test">
        Payment is not required in this test version.
      </div>

      <div className="booking__actions">
        <Button variant="secondary" onClick={onBack} disabled={busy}>
          Back
        </Button>
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
  flow,
  promoInput,
  setPromoInput,
  onCommercialChange,
  onChangeService,
  onBack,
  onConfirm,
}: {
  intent: BookingIntent;
  legs: TripRecommendation["legs"];
  busy: boolean;
  flow: Flow;
  promoInput: string;
  setPromoInput: (v: string) => void;
  onCommercialChange: (body: SetCommercialOptionsRequest) => Promise<void>;
  onChangeService: () => void;
  onBack: () => void;
  onConfirm: () => void;
}) {
  const c = intent.commercial;
  const unknowns = intent.items.filter((i) => i.checked_baggage === "unknown");
  const dates = intent.items
    .map((i) => dayMonth(i.departure))
    .filter((v, idx, a) => a.indexOf(v) === idx);

  return (
    <>
      <span className="eyebrow">Review your journey</span>
      <h2>
        One journey. {intent.items.length} ticket
        {intent.items.length === 1 ? "" : "s"}.
      </h2>

      <dl className="booking__facts">
        <div>
          <dt>Route</dt>
          <dd>{intent.route_cities.join(" → ")}</dd>
        </div>
        <div>
          <dt>Dates</dt>
          <dd>{dates.join(" · ")}</dd>
        </div>
        <div>
          <dt>Travellers</dt>
          <dd>{intent.party_size}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd>
            {c?.service_tier_label ?? "—"}{" "}
            <button className="booking__linkbtn" onClick={onChangeService}>
              change
            </button>
          </dd>
        </div>
      </dl>

      <div className="booking__tickets booking__tickets--tight">
        {intent.items.map((it, i) => {
          const leg = legs[i];
          return (
            <details className="booking__ticket" key={it.sequence}>
              <summary>
                <b>
                  {it.origin_airport} → {it.destination_airport}
                </b>
                <span className="booking__ticket-meta">
                  {dayMonth(it.departure)} · {clockTime(it.departure)}–
                  {clockTime(it.arrival)}
                </span>
                <span className="numeric">{money2(it.price_per_person, it.currency)}</span>
              </summary>
              <dl>
                <div>
                  <dt>Cabin bag</dt>
                  <dd>{it.cabin_baggage}</dd>
                </div>
                <div>
                  <dt>Checked bag</dt>
                  <dd>{it.checked_baggage}</dd>
                </div>
                <div>
                  <dt>Flight</dt>
                  <dd>
                    <AirlineTag
                      code={it.carrier || leg?.operator?.split(" ")[0]}
                      name={it.carrier_name}
                      flightNumber={it.flight_number}
                      operatingCode={it.operating_carrier}
                      operatingName={it.operating_carrier_name}
                    />
                  </dd>
                </div>
              </dl>
            </details>
          );
        })}
      </div>

      {c ? (
        <PriceSummary
          commercial={c}
          currency={intent.currency}
          estimate={intent.trip_estimate}
          travellers={intent.party_size}
          busy={busy}
          promoInput={promoInput}
          setPromoInput={setPromoInput}
          onCommercialChange={onCommercialChange}
        />
      ) : (
        <div className="booking__summary">
          <div>
            <span>Tickets</span>
            <strong>{money(intent.discovered_total, intent.currency)}</strong>
          </div>
        </div>
      )}

      {!intent.price_reconciled && (
        <div className="booking__error">
          We can’t take payment for this journey: the ticket prices don’t add up
          to what we were about to charge. Nothing has been booked.
          {intent.price_issue ? ` (${intent.price_issue})` : ""}
        </div>
      )}

      {unknowns.length > 0 && (
        <div className="booking__summary-note">
          {unknowns.length} leg{unknowns.length === 1 ? "" : "s"} with unknown
          checked-baggage terms — Detoura will not claim a price it cannot stand behind.
        </div>
      )}

      <div className="booking__pay">
        <b>Payment</b>
        <span>Payment is not required in this test version.</span>
      </div>

      <div className="booking__actions">
        <Button variant="secondary" onClick={onBack} disabled={busy}>
          Back
        </Button>
        <Button
          size="lg"
          onClick={onConfirm}
          disabled={busy || !intent.price_reconciled}
        >
          {busy
            ? "Working…"
            : flow === "self_service"
              ? "Prepare my journey"
              : "Confirm journey"}
        </Button>
      </div>
    </>
  );
}

function PriceSummary({
  commercial,
  currency,
  estimate,
  travellers,
  busy,
  promoInput,
  setPromoInput,
  onCommercialChange,
}: {
  commercial: CommercialSummary;
  currency: string;
  estimate: BookingIntent["trip_estimate"];
  travellers: number;
  busy: boolean;
  promoInput: string;
  setPromoInput: (v: string) => void;
  onCommercialChange: (body: SetCommercialOptionsRequest) => Promise<void>;
}) {
  const b = commercial.breakdown;
  const applied = commercial.promo_accepted;
  const accom = estimate?.accommodation ?? 0;
  const transfer = estimate?.transfer ?? 0;
  return (
    <div className="booking__pricebox">
      <dl className="booking__breakdown">
        <div>
          <dt>
            Tickets{travellers > 1 ? ` (${travellers} travellers)` : ""}
          </dt>
          <dd className="numeric">{money2(b.supplier_total, currency)}</dd>
        </div>
        <div>
          <dt>Detoura {commercial.service_tier_label}</dt>
          <dd className="numeric">{money2(b.detoura_revenue_gross, currency)}</dd>
        </div>
        {b.tax > 0 && (
          <div>
            <dt>Tax</dt>
            <dd className="numeric">{money2(b.tax, currency)}</dd>
          </div>
        )}
        {b.discount > 0 && (
          <div className="booking__breakdown-discount">
            <dt>Promo {commercial.promo_code}</dt>
            <dd className="numeric">−{money2(b.discount, currency)}</dd>
          </div>
        )}
        <div className="booking__breakdown-total">
          <dt>Pay now</dt>
          <dd className="numeric">{money2(b.customer_total, currency)}</dd>
        </div>
      </dl>

      {(accom > 0 || transfer > 0) && (
        <dl className="booking__breakdown booking__breakdown--estimate">
          <p className="booking__estimate-h">
            Not booked by Detoura — estimated, you arrange and pay separately
          </p>
          {accom > 0 && (
            <div>
              <dt>Estimated accommodation</dt>
              <dd className="numeric">{money2(accom, currency)}</dd>
            </div>
          )}
          {transfer > 0 && (
            <div>
              <dt>Estimated airport transfers</dt>
              <dd className="numeric">{money2(transfer, currency)}</dd>
            </div>
          )}
        </dl>
      )}

      <div className="booking__promo">
        <div className="booking__promo-row">
          <input
            id="promo"
            value={promoInput}
            disabled={busy || applied}
            placeholder="Promo code"
            aria-label="Promo code"
            onChange={(e) => setPromoInput(e.target.value.toUpperCase())}
          />
          {applied ? (
            <Button
              variant="secondary"
              type="button"
              disabled={busy}
              onClick={() => {
                setPromoInput("");
                void onCommercialChange({ clear_promo: true });
              }}
            >
              Remove
            </Button>
          ) : (
            <Button
              variant="secondary"
              type="button"
              disabled={busy || promoInput.trim().length < 3}
              onClick={() => onCommercialChange({ promo_code: promoInput.trim() })}
            >
              Apply
            </Button>
          )}
        </div>
        {commercial.promo_message && (
          <p className={`booking__promo-msg${applied ? " is-ok" : " is-bad"}`}>
            {commercial.promo_message}
          </p>
        )}
      </div>

      <p className="booking__breakdown-note">
        “Tickets” is the current bookable fare for the flights, exactly as
        quoted. Detoura’s fee is separate. Accommodation is an estimate only —
        Detoura is not booking it.
        {commercial.test_mode ? " Sandbox / test mode — no payment is taken." : ""}
      </p>
    </div>
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

const GUIDED_LABELS: Record<SelfServiceTicket["guided_state"], string> = {
  READY_TO_BOOK: "Ready to book",
  EXTERNAL_BOOKING_STARTED: "Booking started",
  BOOKING_CONFIRMATION_REQUIRED: "Waiting for the airline’s confirmation",
  CONFIRMED: "Booked (you told us)",
  UNKNOWN: "Status unknown",
};

function GuidedStep({
  itinerary,
  busy,
  onStart,
  onMark,
  onDone,
}: {
  itinerary: SelfServiceItinerary;
  busy: boolean;
  onStart: (seq: number) => void;
  onMark: (
    seq: number,
    state: SelfServiceTicket["guided_state"],
    reference?: string,
  ) => void;
  onDone: () => void;
}) {
  return (
    <>
      <span className="eyebrow">Your journey · guided booking</span>
      <h2>{itinerary.headline}</h2>
      <p className="muted">
        Detoura optimised this journey, re-checked the fares and saved your
        traveller details. Book each ticket below — Detoura keeps track. Nothing
        here is booked by Detoura, and a ticket only shows as booked when you
        tell us it is.
      </p>

      <div className="booking__progressbar">
        <b>
          {itinerary.booked_count} of {itinerary.ticket_count} booked
        </b>
        {itinerary.ticket_count - itinerary.booked_count > 0 && (
          <span className="muted">
            {" "}
            · {itinerary.ticket_count - itinerary.booked_count} to go
          </span>
        )}
      </div>

      <div className="booking__guided">
        {itinerary.tickets.map((t) => {
          const done = t.guided_state === "CONFIRMED";
          const started =
            t.guided_state === "EXTERNAL_BOOKING_STARTED" ||
            t.guided_state === "BOOKING_CONFIRMATION_REQUIRED";
          return (
            <div
              key={t.sequence}
              className={`booking__guided-item${done ? " is-done" : started ? " is-active" : ""}`}
            >
              <div className="booking__guided-head">
                <i>{done ? "✓" : started ? "●" : t.sequence}</i>
                <div>
                  <strong>
                    {t.origin_city} → {t.destination_city}
                  </strong>
                  <small>
                    {dayMonth(t.departure)} · {clockTime(t.departure)}–
                    {clockTime(t.arrival)} · {t.carrier || "—"} ·{" "}
                    {money2(t.rechecked_fare ?? t.fare, t.currency)}
                  </small>
                </div>
                <span className={`booking__guided-state s-${t.guided_state}`}>
                  {GUIDED_LABELS[t.guided_state]}
                </span>
              </div>

              {!t.available && (
                <p className="booking__guided-note is-bad">
                  This fare is no longer available — {t.note}
                </p>
              )}

              {t.guided_state === "READY_TO_BOOK" && t.available && (
                <div className="booking__guided-body">
                  <p>{t.booking_guidance}</p>
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => onStart(t.sequence)}
                  >
                    I’ll book this now
                  </Button>
                </div>
              )}

              {started && (
                <div className="booking__guided-body">
                  <p className="muted">
                    Once you’ve booked with the airline, tell Detoura where it
                    stands:
                  </p>
                  <div className="booking__guided-actions">
                    <Button
                      size="sm"
                      disabled={busy}
                      onClick={() => onMark(t.sequence, "CONFIRMED")}
                    >
                      It’s booked
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={busy}
                      onClick={() =>
                        onMark(t.sequence, "BOOKING_CONFIRMATION_REQUIRED")
                      }
                    >
                      Waiting for confirmation
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => onMark(t.sequence, "READY_TO_BOOK")}
                    >
                      Not yet
                    </Button>
                  </div>
                </div>
              )}

              {done && (
                <p className="booking__guided-note">
                  {t.note}
                  {t.detoura_verified ? "" : " Detoura has not independently verified this."}
                </p>
              )}
            </div>
          );
        })}
      </div>

      {itinerary.commercial && (
        <PriceSummaryReadonly
          commercial={itinerary.commercial}
          currency={itinerary.commercial.breakdown.currency}
        />
      )}

      <div className="booking__actions">
        <Button onClick={onDone}>
          {itinerary.booked_count === itinerary.ticket_count
            ? "All done — back to trips"
            : "I’ll finish later"}
        </Button>
      </div>
    </>
  );
}

function PriceSummaryReadonly({
  commercial,
  currency,
}: {
  commercial: CommercialSummary;
  currency: string;
}) {
  const b = commercial.breakdown;
  return (
    <dl className="booking__breakdown">
      <div>
        <dt>Flights</dt>
        <dd className="numeric">{money2(b.supplier_total, currency)}</dd>
      </div>
      <div>
        <dt>Detoura {commercial.service_tier_label}</dt>
        <dd className="numeric">{money2(b.detoura_revenue_gross, currency)}</dd>
      </div>
      {b.discount > 0 && (
        <div className="booking__breakdown-discount">
          <dt>Promo {commercial.promo_code}</dt>
          <dd className="numeric">−{money2(b.discount, currency)}</dd>
        </div>
      )}
      <div className="booking__breakdown-total">
        <dt>Detoura total</dt>
        <dd className="numeric">{money2(b.customer_total, currency)}</dd>
      </div>
    </dl>
  );
}

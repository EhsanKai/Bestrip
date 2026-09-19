import { useEffect, useId, useRef } from "react";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import "./JourneyDrawer.css";

export type JourneyDrawerStatus =
  | "empty"
  | "selected"
  | "checkout"
  | "priceReview"
  | "checking";

interface JourneyStop {
  city: string;
  nights: number;
  dates: string;
}

interface JourneyMoneyLine {
  label: string;
  amount: string;
}

export interface JourneyDrawerModel {
  id: string;
  status: JourneyDrawerStatus;
  stops: JourneyStop[];
  dates: string;
  travelers: string;
  duration: string;
  characteristics: string[];
  payable: JourneyMoneyLine[];
  payableNow: string;
  estimates: JourneyMoneyLine[];
  estimatedTripTotal: string;
}

interface Props {
  open: boolean;
  journey: JourneyDrawerModel | null;
  onClose: () => void;
  onContinueCheckout: () => void;
  onReviewChanges: () => void;
  onExploreJourneys: () => void;
  onViewJourney: () => void;
  onStatusChange: (status: JourneyDrawerStatus) => void;
}

const statusCopy: Record<
  JourneyDrawerStatus,
  { label: string; detail: string; cta: string; tone: "neutral" | "warn" }
> = {
  empty: {
    label: "No journey selected",
    detail: "Your next journey starts with a place worth discovering.",
    cta: "Explore journeys",
    tone: "neutral",
  },
  selected: {
    label: "Not booked yet",
    detail: "This journey is selected, but no checkout or booking has been completed.",
    cta: "View journey",
    tone: "neutral",
  },
  checkout: {
    label: "Checkout started",
    detail: "You can continue checkout. This journey is still not booked.",
    cta: "Continue checkout",
    tone: "neutral",
  },
  priceReview: {
    label: "Price update",
    detail: "One part of your journey has changed since you selected it.",
    cta: "Review changes",
    tone: "warn",
  },
  checking: {
    label: "We're checking your journey",
    detail: "Some journey details still need confirmation before anything can be treated as booked.",
    cta: "View journey",
    tone: "warn",
  },
};

export function JourneyDrawer({
  open,
  journey,
  onClose,
  onContinueCheckout: _onContinueCheckout,
  onReviewChanges,
  onExploreJourneys,
  onViewJourney,
  onStatusChange: _onStatusChange,
}: Props) {
  const titleId = useId();
  const statusId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const effectiveStatus = journey?.status ?? "empty";
  const copy = statusCopy[effectiveStatus];

  useEffect(() => {
    if (!open) return;
    triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.setTimeout(() => closeRef.current?.focus(), 0);

    return () => {
      document.body.style.overflow = originalOverflow;
      triggerRef.current?.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      if (event.key !== "Tab" || !dialogRef.current) return;

      const focusable = dialogRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])',
      );
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) return null;

  function handlePrimary() {
    if (!journey || effectiveStatus === "empty") {
      onExploreJourneys();
      return;
    }
    if (effectiveStatus === "checking") {
      onViewJourney();
      return;
    }
    if (effectiveStatus === "priceReview") {
      onReviewChanges();
      return;
    }
    onViewJourney();
  }

  return (
    <div className="journey-shell" role="presentation">
      <button
        type="button"
        className="journey-shell__backdrop"
        aria-label="Close Your Journey"
        onClick={onClose}
      />
      <section
        ref={dialogRef}
        className="journey-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={statusId}
      >
        <div className="journey-drawer__top">
          <div>
            <p className="journey-drawer__kicker">Your Journey</p>
            <h2 id={titleId}>{journey ? routeTitle(journey) : "Nothing selected yet"}</h2>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="journey-drawer__close"
            onClick={onClose}
            aria-label="Close Your Journey"
          >
            {Icon.close({ size: 19 })}
          </button>
        </div>

        <div
          id={statusId}
          className={`journey-status journey-status--${copy.tone}`}
          role="status"
          aria-live="polite"
        >
          <strong>{copy.label}</strong>
          <span>{copy.detail}</span>
        </div>

        {journey ? (
          <>
            <div className="journey-facts" aria-label="Journey facts">
              <span>{Icon.calendar({ size: 15 })}{journey.dates}</span>
              <span>{Icon.people({ size: 15 })}{journey.travelers}</span>
              <span>{Icon.clock({ size: 15 })}{journey.duration}</span>
            </div>

            <section className="journey-section journey-section--route" aria-labelledby="journey-route-title">
              <h3 id="journey-route-title">Route</h3>
              <ol className="journey-route">
                {journey.stops.map((stop, index) => (
                  <li key={stop.city}>
                    <span className="journey-route__marker" aria-hidden="true" />
                    <span className="journey-route__city">{stop.city}</span>
                    <span className="journey-route__meta">
                      {stop.nights} {stop.nights === 1 ? "night" : "nights"} · {stop.dates}
                    </span>
                    {index < journey.stops.length - 1 && (
                      <span className="journey-route__line" aria-hidden="true" />
                    )}
                  </li>
                ))}
              </ol>
            </section>

            <section className="journey-section journey-section--traits" aria-labelledby="journey-style-title">
              <h3 id="journey-style-title">Journey character</h3>
              <ul className="journey-traits">
                {journey.characteristics.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </section>

            <section className="journey-money journey-money--payable" aria-labelledby="journey-payable-title">
              <h3 id="journey-payable-title">Payable now</h3>
              <p>Checkout has not started, so Detoura has not established a payable amount yet.</p>
              <dl>
                {journey.payable.map((line) => (
                  <div key={line.label}>
                    <dt>{line.label}</dt>
                    <dd>{line.amount}</dd>
                  </div>
                ))}
              </dl>
              <div className="journey-money__total">
                <span>Payable now</span>
                <strong>{journey.payableNow}</strong>
              </div>
            </section>

            <section className="journey-money journey-money--estimate" aria-labelledby="journey-estimate-title">
              <h3 id="journey-estimate-title">Estimated trip costs</h3>
              <p>These are not included in today's payment.</p>
              <dl>
                {journey.estimates.map((line) => (
                  <div key={line.label}>
                    <dt>{line.label}</dt>
                    <dd>{line.amount}</dd>
                  </div>
                ))}
              </dl>
              <div className="journey-money__estimate-total">
                <span>Estimated whole-trip total</span>
                <strong>{journey.estimatedTripTotal}</strong>
              </div>
            </section>

            <button type="button" className="journey-link" onClick={onViewJourney}>
              View journey {Icon.arrowRight({ size: 15 })}
            </button>
          </>
        ) : (
          <div className="journey-empty">
            <span className="journey-empty__mark" aria-hidden="true">{Icon.route({ size: 28 })}</span>
            <p>Your next journey starts with a place worth discovering.</p>
          </div>
        )}

        <div className="journey-drawer__actions">
          <Button full size="lg" onClick={handlePrimary} iconAfter={Icon.arrowRight({ size: 17 })}>
            {copy.cta}
          </Button>
        </div>
      </section>
    </div>
  );
}

function routeTitle(journey: JourneyDrawerModel) {
  return journey.stops.map((stop) => stop.city).join(" to ");
}

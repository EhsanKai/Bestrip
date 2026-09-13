import { useMemo, useState } from "react";
import "./Checkout.css";

type CheckoutState =
  | "ready"
  | "processing"
  | "authenticationRequired"
  | "authorized"
  | "paymentFailed"
  | "paymentUnknown"
  | "recoveryRequired"
  | "success";

const checkout = {
  tier: "ALL_IN_ONE",
  tierLabel: "Detoura handles it",
  journey: {
    route: ["Cologne", "Prague", "Vienna", "Budapest", "Cologne"],
    duration: "8 days",
    cities: "3 cities",
    travelers: "2 travelers",
  },
  travelers: [
    { label: "Traveler 1", type: "Adult" },
    { label: "Traveler 2", type: "Adult" },
  ],
  payable: [
    { label: "Flight tickets", amount: "€420.00" },
    { label: "Detoura service fee", amount: "€27.00" },
  ],
  payableNow: "€447.00",
  estimates: [
    { label: "Accommodation estimate", amount: "€310" },
    { label: "Local transfers estimate", amount: "€45" },
    { label: "Estimated extras", amount: "€30" },
  ],
  estimatedAdditional: "€385",
  estimatedTripTotal: "€832",
};

const stateCopy: Record<CheckoutState, { title: string; body: string; tone: "neutral" | "good" | "warn" | "bad" }> = {
  ready: {
    title: "Ready for payment",
    body: "Review the amount Detoura will charge now before continuing.",
    tone: "neutral",
  },
  processing: {
    title: "Processing payment",
    body: "Please wait. Do not refresh or submit the payment again.",
    tone: "neutral",
  },
  authenticationRequired: {
    title: "Bank authentication may be required",
    body: "Your bank may ask for an additional verification step before authorization completes.",
    tone: "warn",
  },
  authorized: {
    title: "Payment authorized",
    body: "Authorization succeeded. The backend-controlled booking step can proceed next.",
    tone: "good",
  },
  paymentFailed: {
    title: "Payment was not successful",
    body: "No successful payment is shown for this attempt. You can retry when ready.",
    tone: "bad",
  },
  paymentUnknown: {
    title: "We're confirming your payment status",
    body: "Do not retry blindly. Detoura should confirm the payment status before another attempt.",
    tone: "warn",
  },
  recoveryRequired: {
    title: "Detoura is resolving this journey",
    body: "The journey is not confirmed. A recovery flow should determine the payment and booking status.",
    tone: "warn",
  },
  success: {
    title: "Payment and booking confirmed",
    body: "This mock state is only shown when supplied outcome data says both payment and booking succeeded.",
    tone: "good",
  },
};

export function Checkout() {
  const [state, setState] = useState<CheckoutState>("ready");
  const isProcessing = state === "processing";
  const payLabel = useMemo(() => {
    if (isProcessing) return `Processing ${checkout.payableNow}`;
    if (state === "paymentFailed") return `Retry payment ${checkout.payableNow}`;
    return `Confirm and pay ${checkout.payableNow}`;
  }, [isProcessing, state]);
  const message = stateCopy[state];

  return (
    <main className="checkout" aria-labelledby="checkout-title">
      <section className="checkout__hero">
        <p className="checkout__eyebrow">Secure checkout</p>
        <h1 id="checkout-title">Confirm your Detoura journey.</h1>
        <p>
          Pay only the amount due today. Estimated trip costs stay separate so
          the total picture is clear.
        </p>
      </section>

      <div className="checkout__layout">
        <div className="checkout__main">
          <section className="checkout-card" aria-labelledby="journey-title">
            <div className="checkout-card__head">
              <p>Journey</p>
              <button type="button">Review journey</button>
            </div>
            <h2 id="journey-title">{checkout.journey.route.join(" → ")}</h2>
            <dl className="checkout-facts">
              <div><dt>Duration</dt><dd>{checkout.journey.duration}</dd></div>
              <div><dt>Destinations</dt><dd>{checkout.journey.cities}</dd></div>
              <div><dt>Travelers</dt><dd>{checkout.journey.travelers}</dd></div>
            </dl>
          </section>

          <section className="checkout-card" aria-labelledby="travelers-title">
            <div className="checkout-card__head">
              <p>Travelers</p>
              <button type="button">Review traveler details</button>
            </div>
            <h2 id="travelers-title">2 travelers</h2>
            <ul className="checkout-travelers">
              {checkout.travelers.map(traveler => (
                <li key={traveler.label}>
                  <strong>{traveler.label}</strong>
                  <span>{traveler.type}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="checkout-card" aria-labelledby="method-title">
            <h2 id="method-title">Payment method</h2>
            <div className="checkout-payment-slot" role="group" aria-label="Payment provider integration area">
              <strong>Secure payment component will appear here</strong>
              <p>Stripe or another supported provider can mount its approved payment UI in this area.</p>
            </div>
          </section>

          <section className="checkout-card checkout-card--notice" aria-labelledby="info-title">
            <h2 id="info-title">Before you pay</h2>
            <ul>
              <li>Payment may require bank authentication.</li>
              <li>Authorization does not mean every underlying booking is confirmed.</li>
              <li>Estimated accommodation, transfer, and extras are not included in today's payment.</li>
            </ul>
          </section>
        </div>

        <aside className="checkout-summary" aria-labelledby="summary-title">
          <div className="checkout-summary__top">
            <p>{checkout.tierLabel}</p>
            <h2 id="summary-title">Payment summary</h2>
          </div>

          <section aria-labelledby="payable-title" className="checkout-money checkout-money--payable">
            <h3 id="payable-title">Payable now</h3>
            <p>This is the amount Detoura will charge now.</p>
            <dl>
              {checkout.payable.map(item => (
                <div key={item.label}><dt>{item.label}</dt><dd>{item.amount}</dd></div>
              ))}
            </dl>
            <div className="checkout-money__total">
              <span>Payable now</span>
              <strong>{checkout.payableNow}</strong>
            </div>
          </section>

          <button className="checkout-pay" type="button" disabled={isProcessing} onClick={() => setState("processing")}>
            {payLabel}
          </button>
          <p className="checkout-pay__note">Booking continues only through the backend-controlled payment and orchestration flow.</p>

          <section aria-labelledby="estimate-title" className="checkout-money checkout-money--estimate">
            <h3 id="estimate-title">Estimated during your trip</h3>
            <p>These costs are estimates and are not included in today's payment.</p>
            <dl>
              {checkout.estimates.map(item => (
                <div key={item.label}><dt>{item.label}</dt><dd>{item.amount}</dd></div>
              ))}
              <div><dt>Estimated additional trip costs</dt><dd>{checkout.estimatedAdditional}</dd></div>
              <div><dt>Estimated total trip cost</dt><dd>{checkout.estimatedTripTotal}</dd></div>
            </dl>
          </section>

          <div className={`checkout-state checkout-state--${message.tone}`} role="status" aria-live="polite">
            <strong>{message.title}</strong>
            <p>{message.body}</p>
          </div>

          <fieldset className="checkout-demo">
            <legend>Prototype states</legend>
            {Object.keys(stateCopy).map(value => (
              <button
                key={value}
                type="button"
                aria-pressed={state === value}
                onClick={() => setState(value as CheckoutState)}
              >
                {stateLabel(value as CheckoutState)}
              </button>
            ))}
          </fieldset>
        </aside>
      </div>
    </main>
  );
}

function stateLabel(state: CheckoutState) {
  return state
    .replace(/([A-Z])/g, " $1")
    .replace(/^./, char => char.toUpperCase());
}

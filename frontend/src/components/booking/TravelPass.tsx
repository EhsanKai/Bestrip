import type { TravelPass as TravelPassData } from "../../api/types";
import { money, clockTime, dayMonth } from "../../lib/format";
import "./TravelPass.css";

/**
 * The Detoura Test Travel Pass.
 *
 * Every value here comes from the server-owned pass object, which was built
 * from the confirmed BookingItems and the traveller who was entered. This
 * component renders; it does not compute a status, a total or a reference.
 *
 * Three things it keeps visually distinct: a Duffel Test Order (a technical
 * detail), this pass (a Detoura artifact, marked TEST), and a real boarding
 * pass (which this build never produces).
 */
export function TravelPass({ pass }: { pass: TravelPassData }) {
  const recovery = pass.status === "recovery_required";
  const failed = pass.status === "failed";

  return (
    <div className={`pass pass--${pass.status}`}>
      <div className="pass__crest">
        <span className="pass__brand">DETOURA</span>
        <span className="pass__kicker">
          {failed
            ? "Journey not prepared"
            : recovery
              ? "Journey needs attention"
              : "Your journey is ready"}
        </span>
      </div>

      <div className="pass__badge">TEST TRAVEL PASS</div>

      <div className="pass__route">
        {pass.route_cities.map((city, i) => {
          const airport =
            pass.tickets[i]?.origin_airport ??
            pass.tickets[i - 1]?.destination_airport ??
            "";
          return (
            <div className="pass__stop" key={i}>
              <b>{airport}</b>
              <span>{city}</span>
              {i < pass.route_cities.length - 1 && <i className="pass__arrow">→</i>}
            </div>
          );
        })}
      </div>

      <div className="pass__meta">
        <div>
          <dt>Traveller</dt>
          <dd>{pass.traveler_name}</dd>
        </div>
        <div>
          <dt>Journey reference</dt>
          <dd className="pass__ref">{pass.journey_reference}</dd>
        </div>
        <div>
          <dt>Dates</dt>
          <dd>{pass.travel_dates.join(" – ") || "—"}</dd>
        </div>
        <div>
          <dt>{pass.tickets_prepared} of {pass.tickets.length} prepared</dt>
          <dd>{money(pass.trip_total, pass.currency)} total</dd>
        </div>
      </div>

      <ol className="pass__tickets">
        {pass.tickets.map((t) => {
          const ok = t.confirmed;
          return (
            <li key={t.sequence} className={ok ? "is-ok" : "is-bad"}>
              <span className="pass__tick">{ok ? "✓" : "✕"}</span>
              <div className="pass__leg">
                <strong>
                  {t.origin_city} → {t.destination_city}
                </strong>
                <small>
                  {t.origin_airport} · {dayMonth(t.departure)} ·{" "}
                  {clockTime(t.departure)}–{clockTime(t.arrival)} ·{" "}
                  {[t.carrier, t.flight_number].filter(Boolean).join(" ") || "carrier tbc"}
                </small>
                <small className="pass__legbag">
                  cabin {t.cabin_baggage} · checked {t.checked_baggage} ·{" "}
                  {money(t.price_per_person, t.currency)}
                </small>
              </div>
              <span className="pass__legstate">{t.booking_state.toLowerCase().replace(/_/g, " ")}</span>
            </li>
          );
        })}
      </ol>

      {recovery && (
        <div className="pass__recovery">
          Detoura stopped the remaining booking process. This journey is{" "}
          <strong>not</strong> booked. Contact support with your journey
          reference to arrange the rest.
        </div>
      )}

      {pass.provider_order_ids.length > 0 && (
        <details className="pass__technical">
          <summary>Technical detail</summary>
          <p>
            Duffel Test Mode Order{pass.provider_order_ids.length === 1 ? "" : "s"}{" "}
            (sandbox records, not ticket numbers):
          </p>
          <ul>
            {pass.provider_order_ids.map((id) => (
              <li key={id}>
                <code>{id}</code>
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="pass__disclaimer">
        <b>{pass.mode === "demo_only" ? "DEMO ONLY" : "TEST MODE"}</b>
        <span>{pass.mode_note}</span>
        <span>Not a valid boarding pass · No payment collected</span>
      </div>
    </div>
  );
}

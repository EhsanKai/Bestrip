import type { TripRecommendation } from "../../api/types";
import { cityCountLabel, hours, joinCities, money, percent } from "../../lib/format";
import {
  AvailabilityBadge,
  ConfidenceBadge,
  IntensityBadge,
  PriceFreshnessBadge,
} from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Icon } from "../ui/Icon";
import { JourneyPoster } from "./JourneyPoster";
import { variantFor } from "./illustrationVariant";
import { RouteLine } from "./RouteLine";
import "./RecommendationCard.css";

interface Props {
  trip: TripRecommendation;
  saved?: boolean;
  selected?: boolean;
  comparing?: boolean;
  onOpen?: (trip: TripRecommendation) => void;
  onSave?: (trip: TripRecommendation) => void;
  onCompare?: (trip: TripRecommendation) => void;
}

export function RecommendationCard({
  trip,
  saved = false,
  selected = false,
  comparing = false,
  onOpen,
  onSave,
  onCompare,
}: Props) {
  const modes = trip.legs.map((leg) => leg.mode);
  const variant = variantFor(trip.cities);

  return (
    <Card
      as="article"
      interactive
      selected={selected}
      className={`rec rec--${variant}`}
      onClick={() => onOpen?.(trip)}
      aria-label={`${joinCities(trip.cities)}, ${money(trip.total_price, trip.currency)}`}
    >
      <div className="rec__hero">
        <div className="rec__copy">
          <div className="rec__topline">
            <span className="rec__rank">{trip.rank === 1 ? "Detoura pick" : `Option ${trip.rank}`}</span>
            {trip.rank === 1 && <span className="rec__editorial-mark" aria-hidden="true">◆</span>}
          </div>

          <h3 className="rec__title">{joinCities(trip.cities)}</h3>
          <div className="rec__meta">
            <span>{trip.duration_days.toFixed(0)} days</span>
            <span aria-hidden="true">·</span>
            <span>{cityCountLabel(trip.cities.length)}</span>
          </div>

          <RouteLine nodes={trip.route_nodes} modes={modes} cities={trip.cities} compact />

          <div className="rec__trustline" aria-label="Trip confidence and availability">
            <IntensityBadge band={trip.intensity_band} />
            <ConfidenceBadge level={trip.confidence.level} />
            <AvailabilityBadge status={trip.availability} />
            <PriceFreshnessBadge status={trip.price_freshness} />
          </div>

          {trip.why_we_like_it && (
            <div className="rec__reason">
              <div className="rec__reason-label">Why it stands out</div>
              <p>{trip.why_we_like_it}</p>
            </div>
          )}

          {trip.highlights.length > 0 && (
            <ul className="rec__highlights" aria-label="Highlights">
              {trip.highlights.slice(0, 3).map((highlight) => <li key={highlight}>{highlight}</li>)}
            </ul>
          )}
        </div>

        {/* Not aria-hidden as a whole: this panel holds the price, which is
          * the single most important fact on the card. Only the decorative
          * layers inside it are hidden. */}
        <div className="rec__visual">
          <div className="rec__price-block">
            <div className="rec__total numeric">{money(trip.total_price, trip.currency)}</div>
            <div className="rec__pp numeric">{money(trip.price_per_person, trip.currency)} each</div>
          </div>
          <div className="rec__city-index" aria-hidden="true">
            {trip.cities.map(cityCode).join(" / ")}
          </div>
          <JourneyPoster cities={trip.cities} rank={trip.rank} />
        </div>
      </div>

      <div className="rec__metrics">
        <Metric label="Usable time" value={hours(trip.usable_hours)} />
        <Metric label="In transit" value={hours(trip.travel_hours)} />
        <Metric label="Experience" value={percent(trip.experience_score)} />
        <Metric label="Your interests" value={percent(trip.preference_match)} />
      </div>

      <div className="rec__footer">
        <div className="rec__costs">
          <span><small>Transport</small>{money(trip.costs.transport, trip.currency)}</span>
          <span><small>Rooms</small>{money(trip.costs.accommodation, trip.currency)}</span>
          <span><small>Transfers</small>{money(trip.costs.ground_transfer, trip.currency)}</span>
        </div>

        {trip.tradeoff && <p className="rec__tradeoff">{trip.tradeoff}</p>}

        <div className="rec__actions" onClick={(event) => event.stopPropagation()}>
          <button
            type="button"
            className={`rec__compare ${comparing ? "is-on" : ""}`}
            onClick={() => onCompare?.(trip)}
            aria-pressed={comparing}
          >
            {Icon.compare({ size: 15 })}
            {comparing ? "Comparing" : "Compare"}
          </button>
          <Button onClick={() => onOpen?.(trip)} iconAfter={Icon.arrowRight({ size: 16 })}>
            Explore trip
          </Button>
          <button
            type="button"
            className={`rec__save ${saved ? "rec__save--on" : ""}`}
            onClick={() => onSave?.(trip)}
            aria-pressed={saved}
            aria-label={saved ? "Remove from saved" : "Save this trip"}
          >
            {saved ? Icon.heartFilled({ size: 18 }) : Icon.heart({ size: 18 })}
          </button>
        </div>
      </div>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rec__metric"><span>{label}</span><strong className="numeric">{value}</strong></div>;
}

function cityCode(city: string) {
  const letters = city.replace(/[^A-Za-z]/g, "").toUpperCase();
  return letters.slice(0, 3) || city.slice(0, 3).toUpperCase();
}

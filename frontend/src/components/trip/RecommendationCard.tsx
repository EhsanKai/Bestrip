import type { TripRecommendation } from "../../api/types";
import { cityCountLabel, hours, joinCities, money, percent } from "../../lib/format";
import {
  AvailabilityBadge,
  Badge,
  ConfidenceBadge,
  IntensityBadge,
  PriceFreshnessBadge,
} from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Icon } from "../ui/Icon";
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

/**
 * The card the whole product is judged on.
 *
 * Its job is to make one trip understandable in about three seconds, and the
 * information order is the argument: what it is, what it costs, what you
 * actually get, why we picked it, what it costs you relative to your own idea.
 *
 * The trade-off line is not a disclaimer bolted on the end - it is the point.
 * Detoura's claim is "here is what else your budget buys", and a card that
 * showed only the upside would be selling rather than advising.
 */
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

  return (
    <Card
      as="article"
      interactive
      selected={selected}
      className="rec"
      onClick={() => onOpen?.(trip)}
      aria-label={`${joinCities(trip.cities)}, ${money(trip.total_price, trip.currency)}`}
    >
      <div className="rec__top">
        <div className="rec__identity">
          <div className="rec__rank eyebrow">
            {trip.rank === 1 ? "Best match" : `Option ${trip.rank}`}
          </div>
          <h3 className="rec__title">{joinCities(trip.cities)}</h3>
          <div className="rec__meta muted">
            {trip.duration_days.toFixed(0)} days · {cityCountLabel(trip.cities.length)}
          </div>
        </div>

        <div className="rec__price">
          <div className="rec__total numeric">{money(trip.total_price, trip.currency)}</div>
          <div className="rec__pp subtle numeric">
            {money(trip.price_per_person, trip.currency)} each
          </div>
        </div>
      </div>

      <RouteLine
        nodes={trip.route_nodes}
        modes={modes}
        cities={trip.cities}
        compact
      />

      {/* The four numbers a traveler actually compares trips on. Usable time
       * comes first because it is the one a flight search never shows. */}
      <dl className="rec__stats">
        <div className="rec__stat">
          <dt>Usable time</dt>
          <dd className="numeric">{hours(trip.usable_hours)}</dd>
        </div>
        <div className="rec__stat">
          <dt>In transit</dt>
          <dd className="numeric">{hours(trip.travel_hours)}</dd>
        </div>
        <div className="rec__stat">
          <dt>Experience</dt>
          <dd className="numeric">{percent(trip.experience_score)}</dd>
        </div>
        <div className="rec__stat">
          <dt>Your interests</dt>
          <dd className="numeric">{percent(trip.preference_match)}</dd>
        </div>
      </dl>

      <div className="rec__badges">
        <IntensityBadge band={trip.intensity_band} />
        <ConfidenceBadge level={trip.confidence.level} />
        <AvailabilityBadge status={trip.availability} />
        <PriceFreshnessBadge status={trip.price_freshness} />
      </div>

      {trip.why_we_like_it && (
        <div className="rec__section">
          <div className="eyebrow">Why we like it</div>
          <p className="rec__prose">{trip.why_we_like_it}</p>
        </div>
      )}

      {trip.tradeoff && (
        <div className="rec__section rec__section--tradeoff">
          <div className="eyebrow">Trade-off</div>
          <p className="rec__prose">{trip.tradeoff}</p>
        </div>
      )}

      {trip.highlights.length > 0 && (
        <ul className="rec__highlights">
          {trip.highlights.map((highlight) => (
            <li key={highlight}>
              <Badge tone="neutral">{highlight}</Badge>
            </li>
          ))}
        </ul>
      )}

      <div className="rec__costs subtle">
        <span>Transport {money(trip.costs.transport, trip.currency)}</span>
        <span>Rooms {money(trip.costs.accommodation, trip.currency)}</span>
        <span>Transfers {money(trip.costs.ground_transfer, trip.currency)}</span>
      </div>

      <div className="rec__actions" onClick={(event) => event.stopPropagation()}>
        <Button onClick={() => onOpen?.(trip)} iconAfter={Icon.arrowRight({ size: 16 })}>
          Explore trip
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={() => onCompare?.(trip)}
          aria-pressed={comparing}
          icon={Icon.compare({ size: 16 })}
        >
          {comparing ? "Comparing" : "Compare"}
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
    </Card>
  );
}

import type { TripRecommendation } from "../api/types";
import { RouteMap } from "../components/trip/RouteMap";
import { Timeline } from "../components/trip/Timeline";
import {
  AvailabilityBadge,
  Badge,
  ConfidenceBadge,
  IntensityBadge,
  PriceFreshnessBadge,
} from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Icon } from "../components/ui/Icon";
import { Score } from "../components/ui/Score";
import { hours, joinCities, matchBand, money, percent } from "../lib/format";
import "./TripDetail.css";

interface Props {
  trip: TripRecommendation;
  saved: boolean;
  /** The traveller's own city, so the map can draw the whole loop. */
  origin?: string;
  onBack: () => void;
  onSave: (trip: TripRecommendation) => void;
}

/**
 * The trip in full (Parts 12-13).
 *
 * Desktop is the three-column layout the spec asks for: itinerary on the left,
 * map in the centre, summary on the right. On a phone that becomes one column
 * in reading order — map, summary, itinerary — because a timeline is what you
 * scroll and a map is what you glance at.
 */
export function TripDetail({ trip, saved, origin, onBack, onSave }: Props) {
  // Part 12 wants the full loop - Cologne to Munich to Vienna and home again -
  // not just the destinations. A route that does not return is not a trip.
  const loop = origin ? [origin, ...trip.cities, origin] : trip.cities;
  return (
    <div className="detail">
      <div className="container">
        <button className="detail__back" onClick={onBack}>
          <span aria-hidden="true">←</span> Back to results
        </button>

        <header className="detail__header">
          <div>
            <h1 className="h1">{joinCities(trip.cities)}</h1>
            <p className="detail__sub muted">
              {trip.duration_days.toFixed(0)} days · {trip.cities.length}{" "}
              {trip.cities.length === 1 ? "city" : "cities"} ·{" "}
              {trip.route_nodes[0]} → {trip.route_nodes[trip.route_nodes.length - 1]}
            </p>
          </div>
          <div className="detail__price-block">
            <div className="detail__price numeric">
              {money(trip.total_price, trip.currency)}
            </div>
            <div className="subtle numeric">
              {money(trip.price_per_person, trip.currency)} per person
            </div>
          </div>
        </header>

        <div className="detail__badges">
          <ConfidenceBadge level={trip.confidence.level} />
          <IntensityBadge band={trip.intensity_band} />
          <AvailabilityBadge status={trip.availability} />
          <PriceFreshnessBadge status={trip.price_freshness} />
        </div>

        <div className="detail__layout">
          <div className="detail__timeline">
            <h2 className="h3 detail__section-title">Day by day</h2>
            <Timeline trip={trip} />
          </div>

          <div className="detail__map">
            <Card padded={false} className="detail__map-card">
              <RouteMap
                routes={[{ nodes: loop, highlight: true }]}
                height={340}
                compact
              />
            </Card>

            <Card className="detail__why">
              <h2 className="h3">Why we like it</h2>
              <p>{trip.why_we_like_it}</p>
              {trip.tradeoff && (
                <>
                  <h3 className="eyebrow detail__tradeoff-title">Trade-off</h3>
                  <p className="detail__tradeoff">{trip.tradeoff}</p>
                </>
              )}
            </Card>
          </div>

          <aside className="detail__summary">
            <Card className="detail__panel">
              <h2 className="h3">What you get</h2>
              <Score label="Experience" value={trip.experience_score} />
              <Score
                label="Match for your interests"
                value={trip.preference_match}
                qualitative
              />
              <Score label="Accommodation" value={trip.accommodation_score} tone="sage" />
              <dl className="detail__facts">
                <div>
                  <dt>Usable time</dt>
                  <dd className="numeric">{hours(trip.usable_hours)}</dd>
                </div>
                <div>
                  <dt>In transit</dt>
                  <dd className="numeric">{hours(trip.travel_hours)}</dd>
                </div>
                <div>
                  <dt>Airport transfers</dt>
                  <dd className="numeric">{trip.transfer_minutes} min</dd>
                </div>
              </dl>
            </Card>

            <Card className="detail__panel">
              <h2 className="h3">Where the money goes</h2>
              <ul className="detail__costs">
                <CostRow
                  label="Transport"
                  value={trip.costs.transport}
                  total={trip.costs.total}
                  currency={trip.currency}
                />
                <CostRow
                  label="Accommodation"
                  value={trip.costs.accommodation}
                  total={trip.costs.total}
                  currency={trip.currency}
                />
                <CostRow
                  label="Airport transfers"
                  value={trip.costs.ground_transfer}
                  total={trip.costs.total}
                  currency={trip.currency}
                />
              </ul>
              <div className="detail__total">
                <span>Total</span>
                <span className="numeric">{money(trip.costs.total, trip.currency)}</span>
              </div>
            </Card>

            {trip.destination_matches.length > 0 && (
              <Card className="detail__panel">
                <h2 className="h3">Why these places</h2>
                {trip.destination_matches.map((match) => (
                  <div key={match.city} className="detail__match">
                    <div className="detail__match-head">
                      <strong>{match.city}</strong>
                      <span className="subtle">{matchBand(match.match)}</span>
                    </div>
                    {match.strengths.length > 0 && (
                      <div className="detail__match-tags">
                        {match.strengths.map((strength) => (
                          <Badge key={strength} tone="sage">
                            {strength.replace("_", " ")}
                          </Badge>
                        ))}
                      </div>
                    )}
                    <p className="subtle detail__match-note">{match.note}</p>
                  </div>
                ))}
              </Card>
            )}

            <Card className="detail__panel">
              <h2 className="h3">{trip.confidence.label}</h2>
              <ul className="detail__reasons">
                {trip.confidence.reasons.map((reason) => (
                  <li
                    key={reason.label}
                    className={reason.positive ? "is-positive" : "is-negative"}
                  >
                    <span aria-hidden="true">
                      {reason.positive ? Icon.check({ size: 14 }) : Icon.alert({ size: 14 })}
                    </span>
                    {reason.label}
                  </li>
                ))}
              </ul>
            </Card>
          </aside>
        </div>

        <div className="detail__sticky">
          <div className="detail__sticky-price">
            <span className="numeric">{money(trip.total_price, trip.currency)}</span>
            <span className="subtle">
              {trip.duration_days.toFixed(0)} days · {percent(trip.preference_match)} match
            </span>
          </div>
          <Button
            variant={saved ? "secondary" : "primary"}
            size="lg"
            onClick={() => onSave(trip)}
            icon={saved ? Icon.heartFilled({ size: 18 }) : Icon.heart({ size: 18 })}
          >
            {saved ? "Saved" : "Save this trip"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function CostRow({
  label,
  value,
  total,
  currency,
}: {
  label: string;
  value: number;
  total: number;
  currency: string;
}) {
  const share = total > 0 ? value / total : 0;
  return (
    <li className="detail__cost">
      <div className="detail__cost-head">
        <span>{label}</span>
        <span className="numeric">{money(value, currency)}</span>
      </div>
      <div className="detail__cost-bar">
        <div style={{ width: `${share * 100}%` }} />
      </div>
    </li>
  );
}

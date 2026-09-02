import type { TripRecommendation } from "../api/types";
import { RouteLine } from "../components/trip/RouteLine";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { hours, joinCities, money, percent } from "../lib/format";
import "./Compare.css";

interface Props {
  trips: TripRecommendation[];
  onBack: () => void;
  onOpen: (trip: TripRecommendation) => void;
  onRemove: (trip: TripRecommendation) => void;
}

type Metric = {
  key: string;
  label: string;
  value: (trip: TripRecommendation) => number;
  render: (trip: TripRecommendation) => string;
  /** Which direction wins. */
  best: "high" | "low";
  winnerLabel: string;
};

const METRICS: Metric[] = [
  {
    key: "price",
    label: "Total price",
    value: (t) => t.total_price,
    render: (t) => money(t.total_price, t.currency),
    best: "low",
    winnerLabel: "Best price",
  },
  {
    key: "cities",
    label: "Cities",
    value: (t) => t.cities.length,
    render: (t) => String(t.cities.length),
    best: "high",
    winnerLabel: "Most cities",
  },
  {
    key: "usable",
    label: "Usable time",
    value: (t) => t.usable_hours,
    render: (t) => hours(t.usable_hours),
    best: "high",
    winnerLabel: "Most usable time",
  },
  {
    key: "transit",
    label: "In transit",
    value: (t) => t.travel_hours,
    render: (t) => hours(t.travel_hours),
    best: "low",
    winnerLabel: "Least transit",
  },
  {
    key: "experience",
    label: "Experience",
    value: (t) => t.experience_score,
    render: (t) => percent(t.experience_score),
    best: "high",
    winnerLabel: "Best experience",
  },
  {
    key: "match",
    label: "Your interests",
    value: (t) => t.preference_match,
    render: (t) => percent(t.preference_match),
    best: "high",
    winnerLabel: "Best match",
  },
  {
    key: "rooms",
    label: "Accommodation",
    value: (t) => t.accommodation_score,
    render: (t) => percent(t.accommodation_score),
    best: "high",
    winnerLabel: "Best rooms",
  },
  {
    key: "intensity",
    label: "Travel intensity",
    value: (t) => t.travel_intensity,
    render: (t) => t.intensity_band.toLowerCase(),
    best: "low",
    winnerLabel: "Most relaxed",
  },
];

/**
 * Compare mode (Part 11).
 *
 * The spec asks explicitly for visual emphasis over an Excel grid, and the
 * distinction it is really after is this: a table shows you sixteen numbers
 * and leaves the reading to you. Here every row *names its winner* — "Best
 * price", "Most usable time" — so the comparison resolves into a handful of
 * claims instead of a wall of digits. Ties are left unmarked rather than
 * awarded to whichever column came first.
 */
export function Compare({ trips, onBack, onOpen, onRemove }: Props) {
  if (trips.length < 2) {
    return (
      <div className="container compare__empty">
        <h1 className="h2">Pick at least two trips to compare</h1>
        <Button onClick={onBack}>Back to results</Button>
      </div>
    );
  }

  function winnerFor(metric: Metric): string | null {
    const values = trips.map(metric.value);
    const target = metric.best === "high" ? Math.max(...values) : Math.min(...values);
    const holders = trips.filter((trip) => metric.value(trip) === target);
    // A metric everything ties on tells the traveler nothing, so it wins
    // nothing.
    return holders.length === 1 ? holders[0].id : null;
  }

  return (
    <div className="compare">
      <div className="container">
        <button className="compare__back" onClick={onBack}>
          <span aria-hidden="true">←</span> Back to results
        </button>
        <h1 className="h1 compare__title">Side by side</h1>

        <div
          className="compare__grid"
          style={{ gridTemplateColumns: `minmax(120px, 0.7fr) repeat(${trips.length}, minmax(0, 1fr))` }}
        >
          <div className="compare__corner" />
          {trips.map((trip) => (
            <Card key={trip.id} className="compare__head">
              <h2 className="compare__name">{joinCities(trip.cities)}</h2>
              <RouteLine
                nodes={trip.route_nodes}
                cities={trip.cities}
                modes={trip.legs.map((l) => l.mode)}
                compact
              />
              <div className="compare__head-actions">
                <Button size="sm" onClick={() => onOpen(trip)}>
                  Open
                </Button>
                <Button size="sm" variant="ghost" onClick={() => onRemove(trip)}>
                  Remove
                </Button>
              </div>
            </Card>
          ))}

          {METRICS.map((metric) => {
            const winner = winnerFor(metric);
            return (
              <div className="compare__row" key={metric.key}>
                <div className="compare__label">{metric.label}</div>
                {trips.map((trip) => {
                  const isWinner = winner === trip.id;
                  return (
                    <div
                      key={trip.id}
                      className={`compare__cell ${isWinner ? "compare__cell--win" : ""}`}
                    >
                      <span className="compare__value numeric">{metric.render(trip)}</span>
                      {isWinner && (
                        <Badge tone="accent">{metric.winnerLabel}</Badge>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

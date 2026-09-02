import type { TripRecommendation } from "../api/types";
import type { SavedTrip } from "../state/useSaved";
import { RouteLine } from "../components/trip/RouteLine";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Icon } from "../components/ui/Icon";
import { dayMonth, hours, joinCities, money, signedMoney } from "../lib/format";
import "./SavedTrips.css";

interface Props {
  trips: SavedTrip[];
  onOpen: (trip: TripRecommendation) => void;
  onRemove: (trip: TripRecommendation) => void;
  onDiscover: () => void;
}

/**
 * Saved trips (Part 17).
 *
 * The price-change line is deliberately absent until there is a re-check to
 * report. Comparing a saved price against itself and announcing "no change"
 * would imply we have been watching, and we have not — a saved trip is a
 * snapshot until something re-prices it.
 */
export function SavedTrips({ trips, onOpen, onRemove, onDiscover }: Props) {
  if (trips.length === 0) {
    return (
      <div className="container saved__empty">
        <span className="saved__empty-icon">{Icon.heart({ size: 32 })}</span>
        <h1 className="h2">Nothing saved yet</h1>
        <p className="lead">
          Save a trip and it'll wait here — with its price, its route and
          everything we worked out about it.
        </p>
        <Button size="lg" onClick={onDiscover}>
          Find your detour
        </Button>
      </div>
    );
  }

  return (
    <div className="saved">
      <div className="container">
        <h1 className="h1 saved__title">Saved trips</h1>
        <ul className="saved__list">
          {trips.map((trip) => {
            const change = trip.total_price - trip.saved_price;
            return (
              <li key={trip.id}>
                <Card interactive className="saved__card" onClick={() => onOpen(trip)}>
                  <div className="saved__main">
                    <h2 className="saved__name">{joinCities(trip.cities)}</h2>
                    <RouteLine
                      nodes={trip.route_nodes}
                      cities={trip.cities}
                      modes={trip.legs.map((leg) => leg.mode)}
                      compact
                    />
                    <div className="saved__meta subtle">
                      <span>Saved {dayMonth(trip.saved_at)}</span>
                      <span aria-hidden="true">·</span>
                      <span className="numeric">{hours(trip.usable_hours)} usable</span>
                    </div>
                  </div>

                  <div className="saved__price-block">
                    <div className="saved__price numeric">
                      {money(trip.total_price, trip.currency)}
                    </div>
                    {change !== 0 && (
                      <div
                        className={`saved__change ${change < 0 ? "is-down" : "is-up"}`}
                      >
                        {signedMoney(change, trip.currency)}
                      </div>
                    )}
                    <button
                      className="saved__remove"
                      onClick={(event) => {
                        event.stopPropagation();
                        onRemove(trip);
                      }}
                      aria-label={`Remove ${joinCities(trip.cities)}`}
                    >
                      {Icon.close({ size: 15 })}
                    </button>
                  </div>
                </Card>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

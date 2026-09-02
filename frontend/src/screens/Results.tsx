import { useMemo, useState } from "react";
import type {
  ProfileName,
  TripRecommendation,
  TripSearchRequest,
  TripSearchResponse,
} from "../api/types";
import { BaselineComparison } from "../components/trip/BaselineComparison";
import { RecommendationCard } from "../components/trip/RecommendationCard";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Icon } from "../components/ui/Icon";
import { ProviderIssues } from "../components/search/ProviderIssues";
import { NoResults } from "../components/search/NoResults";
import { PROFILE_LABELS, cityCountLabel, money } from "../lib/format";
import "./Results.css";

interface Props {
  request: TripSearchRequest;
  response: TripSearchResponse;
  saved: string[];
  comparing: string[];
  deeperPending: boolean;
  onOpen: (trip: TripRecommendation) => void;
  onSave: (trip: TripRecommendation) => void;
  onCompare: (trip: TripRecommendation) => void;
  onOpenCompare: () => void;
  onSearchDeeper: () => void;
  onProfileChange: (profile: ProfileName) => void;
  onRelax: (patch: Partial<TripSearchRequest>) => void;
  onEdit: () => void;
}

type SortKey = "recommended" | "price" | "usable" | "experience";

/**
 * The most important screen (Part 8).
 *
 * Structure is the argument: the baseline comparison comes *before* the list,
 * because Detoura's claim is relative — "here is what else your budget buys" —
 * and a list of prices without that frame is just a search engine.
 */
export function Results({
  request,
  response,
  saved,
  comparing,
  deeperPending,
  onOpen,
  onSave,
  onCompare,
  onOpenCompare,
  onSearchDeeper,
  onProfileChange,
  onRelax,
  onEdit,
}: Props) {
  const [sort, setSort] = useState<SortKey>("recommended");
  const [maxCities, setMaxCities] = useState<number | null>(null);

  const trips = useMemo(() => {
    let list = [...response.recommendations];
    if (maxCities !== null) {
      list = list.filter((trip) => trip.cities.length <= maxCities);
    }
    if (sort === "price") list.sort((a, b) => a.total_price - b.total_price);
    if (sort === "usable") list.sort((a, b) => b.usable_hours - a.usable_hours);
    if (sort === "experience")
      list.sort((a, b) => b.experience_score - a.experience_score);
    return list;
  }, [response.recommendations, sort, maxCities]);

  const best = response.recommendations[0];

  return (
    <div className="results">
      <div className="container">
        <header className="results__header">
          <div>
            <h1 className="h1">Your best detours</h1>
            <div className="results__summary muted">
              <span>{request.origin}</span>
              <Dot />
              <span>{request.duration_days} days</span>
              <Dot />
              <span>
                {request.travelers}{" "}
                {request.travelers === 1 ? "traveller" : "travellers"}
              </span>
              <Dot />
              <span className="numeric">{money(request.budget)}</span>
              <Dot />
              <span>{PROFILE_LABELS[response.profile]}</span>
            </div>
          </div>
          <Button variant="secondary" onClick={onEdit} icon={Icon.sliders({ size: 16 })}>
            Change search
          </Button>
        </header>

        {/* Degraded data is a banner over real results, never an error page:
         * the trips below are genuine, they were just found with less
         * information than we wanted. */}
        {response.issues.length > 0 && <ProviderIssues issues={response.issues} />}

        {response.no_results ? (
          <NoResults guidance={response.no_results} onRelax={onRelax} />
        ) : (
          <>
            {response.baseline && best && (
              <BaselineComparison
                baseline={response.baseline}
                ourCities={best.cities}
                ourPrice={best.total_price}
                ourUsableHours={best.usable_hours}
                currency={response.currency}
              />
            )}

            <div className="results__toolbar">
              <div className="results__profiles" role="group" aria-label="Trip style">
                {(["CHEAPEST", "BEST_VALUE", "ADVENTURE"] as ProfileName[]).map(
                  (name) => (
                    <button
                      key={name}
                      type="button"
                      className={`results__profile ${
                        response.profile === name ? "is-on" : ""
                      }`}
                      onClick={() => onProfileChange(name)}
                      aria-pressed={response.profile === name}
                    >
                      {PROFILE_LABELS[name]}
                    </button>
                  ),
                )}
              </div>

              <div className="results__filters">
                <label className="results__select">
                  <span className="sr-only">Sort by</span>
                  <select
                    value={sort}
                    onChange={(event) => setSort(event.target.value as SortKey)}
                  >
                    <option value="recommended">Recommended</option>
                    <option value="price">Lowest price</option>
                    <option value="usable">Most usable time</option>
                    <option value="experience">Best experience</option>
                  </select>
                </label>
                <label className="results__select">
                  <span className="sr-only">Maximum cities</span>
                  <select
                    value={maxCities ?? ""}
                    onChange={(event) =>
                      setMaxCities(
                        event.target.value ? Number(event.target.value) : null,
                      )
                    }
                  >
                    <option value="">Any number of cities</option>
                    <option value="1">1 city</option>
                    <option value="2">Up to 2 cities</option>
                    <option value="3">Up to 3 cities</option>
                  </select>
                </label>
                {comparing.length >= 2 && (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={onOpenCompare}
                    icon={Icon.compare({ size: 15 })}
                  >
                    Compare {comparing.length}
                  </Button>
                )}
              </div>
            </div>

            <div className="results__count eyebrow">
              {trips.length} {trips.length === 1 ? "trip" : "trips"} worth considering
              {maxCities !== null && ` · ${cityCountLabel(maxCities)} or fewer`}
            </div>

            <ul className="results__list">
              {trips.map((trip, index) => (
                <li
                  key={trip.id}
                  className="fade-up"
                  // Capped: the stagger is a flourish on the first few, not a queue.
                  style={{ animationDelay: `${Math.min(index, 4) * 55}ms` }}
                >
                  <RecommendationCard
                    trip={trip}
                    saved={saved.includes(trip.id)}
                    comparing={comparing.includes(trip.id)}
                    onOpen={onOpen}
                    onSave={onSave}
                    onCompare={onCompare}
                  />
                </li>
              ))}
            </ul>

            {response.diagnostics.deeper_search_available && (
              <Card className="results__deeper">
                <div>
                  <h2 className="h3">
                    We found {response.recommendations.length} strong trips.
                  </h2>
                  <p className="muted">
                    Search deeper for less obvious alternatives? Takes about
                    10–15 seconds.
                  </p>
                </div>
                <Button
                  variant="secondary"
                  size="lg"
                  loading={deeperPending}
                  onClick={onSearchDeeper}
                  icon={Icon.sparkles({ size: 18 })}
                >
                  {deeperPending ? "Searching deeper…" : "Search deeper"}
                </Button>
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Dot() {
  return (
    <span aria-hidden="true" className="results__dot">
      ·
    </span>
  );
}

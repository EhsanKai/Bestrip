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
import { PROFILE_LABELS, money } from "../lib/format";
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

const SORT_OPTIONS: Array<{ key: SortKey; label: string }> = [
  { key: "recommended", label: "Best match" },
  { key: "price", label: "Lowest price" },
  { key: "usable", label: "Most usable time" },
  { key: "experience", label: "Best experience" },
];

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
    if (maxCities !== null) list = list.filter((trip) => trip.cities.length <= maxCities);
    if (sort === "price") list.sort((a, b) => a.total_price - b.total_price);
    if (sort === "usable") list.sort((a, b) => b.usable_hours - a.usable_hours);
    if (sort === "experience") list.sort((a, b) => b.experience_score - a.experience_score);
    return list;
  }, [response.recommendations, sort, maxCities]);

  const best = response.recommendations[0];
  const interests = request.interests?.length ? request.interests.join(", ") : "Open to anything";

  return (
    <div className="results">
      <div className="container results__shell">
        <aside className="results__rail" aria-label="Search summary and sorting">
          <section className="results__search-card">
            <div className="results__rail-head">
              <span>Your search</span>
              <button type="button" className="results__edit-link" onClick={onEdit}>Edit</button>
            </div>
            <dl className="results__search-facts">
              <div><dt>From</dt><dd>{request.origin}</dd></div>
              <div><dt>Dates</dt><dd>{formatDateRange(request.date_from, request.date_to)}</dd></div>
              <div><dt>Travellers</dt><dd>{request.travelers} {request.travelers === 1 ? "adult" : "adults"}</dd></div>
              <div><dt>Budget</dt><dd className="numeric">{money(request.budget)}</dd></div>
              <div><dt>Mode</dt><dd>{PROFILE_LABELS[response.profile]}</dd></div>
              <div><dt>Interests</dt><dd>{interests}</dd></div>
            </dl>
          </section>

          <nav className="results__sort" aria-label="Sort results">
            <div className="eyebrow results__sort-label">Sort by</div>
            {SORT_OPTIONS.map((option) => (
              <button
                key={option.key}
                type="button"
                className={`results__sort-item ${sort === option.key ? "is-on" : ""}`}
                onClick={() => setSort(option.key)}
                aria-pressed={sort === option.key}
              >
                <span className="results__sort-mark" aria-hidden="true" />
                {option.label}
              </button>
            ))}
          </nav>

          <div className="results__tip">
            <span className="results__tip-kicker">A better detour</span>
            <p>Flexible by a day or two can unlock stronger routes and lower prices.</p>
          </div>
        </aside>

        <main className="results__content">
          <header className="results__header">
            <div>
              <div className="eyebrow results__kicker">Curated for your search</div>
              <h1 className="results__title">Your best detours</h1>
              <div className="results__summary muted">
                <span>{request.duration_days} days</span><Dot />
                <span>{request.travelers} {request.travelers === 1 ? "traveller" : "travellers"}</span><Dot />
                <span className="numeric">{money(request.budget)}</span><Dot />
                <span>{PROFILE_LABELS[response.profile]}</span>
              </div>
            </div>

            <div className="results__header-actions">
              <div className="results__profiles" role="group" aria-label="Trip style">
                {(["CHEAPEST", "BEST_VALUE", "ADVENTURE"] as ProfileName[]).map((name) => (
                  <button
                    key={name}
                    type="button"
                    className={`results__profile ${response.profile === name ? "is-on" : ""}`}
                    onClick={() => onProfileChange(name)}
                    aria-pressed={response.profile === name}
                  >
                    {PROFILE_LABELS[name]}
                  </button>
                ))}
              </div>
            </div>
          </header>

          {response.issues.length > 0 && <ProviderIssues issues={response.issues} />}

          {response.no_results ? (
            <NoResults guidance={response.no_results} onRelax={onRelax} />
          ) : (
            <>
              {response.baseline && best && (
                <div className="results__baseline-wrap">
                  <BaselineComparison
                    baseline={response.baseline}
                    ourCities={best.cities}
                    ourPrice={best.total_price}
                    ourUsableHours={best.usable_hours}
                    currency={response.currency}
                  />
                </div>
              )}

              <div className="results__controls">
                <div className="results__count">
                  <strong>{trips.length}</strong> {trips.length === 1 ? "trip" : "trips"} worth considering
                </div>
                <div className="results__filters">
                  <label className="results__select">
                    <span className="sr-only">Maximum cities</span>
                    <select value={maxCities ?? ""} onChange={(event) => setMaxCities(event.target.value ? Number(event.target.value) : null)}>
                      <option value="">Any number of cities</option>
                      <option value="1">1 city</option>
                      <option value="2">Up to 2 cities</option>
                      <option value="3">Up to 3 cities</option>
                    </select>
                  </label>
                  {comparing.length >= 2 && (
                    <Button size="sm" variant="secondary" onClick={onOpenCompare} icon={Icon.compare({ size: 15 })}>
                      Compare {comparing.length}
                    </Button>
                  )}
                </div>
              </div>

              <ul className="results__list">
                {trips.map((trip, index) => (
                  <li key={trip.id} className="fade-up" style={{ animationDelay: `${Math.min(index, 4) * 45}ms` }}>
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
                    <div className="eyebrow">Deep discovery</div>
                    <h2 className="h3">Search beyond the obvious.</h2>
                    <p className="muted">We found {response.recommendations.length} strong trips. A deeper pass can uncover less obvious alternatives in about 10–15 seconds.</p>
                  </div>
                  <Button variant="secondary" size="lg" loading={deeperPending} onClick={onSearchDeeper} icon={Icon.sparkles({ size: 18 })}>
                    {deeperPending ? "Searching deeper…" : "Search deeper"}
                  </Button>
                </Card>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}

function Dot() {
  return <span aria-hidden="true" className="results__dot">·</span>;
}

function formatDateRange(from: string, to: string) {
  try {
    const fmt = new Intl.DateTimeFormat("en", { month: "short", day: "numeric" });
    return `${fmt.format(new Date(`${from}T12:00:00`))} – ${fmt.format(new Date(`${to}T12:00:00`))}`;
  } catch {
    return `${from} – ${to}`;
  }
}

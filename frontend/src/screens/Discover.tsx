import { useEffect, useState } from "react";
import { api } from "../api/client";
import type {
  Interest,
  OriginAirport,
  ProfileName,
  SearchMode,
  TripSearchRequest,
} from "../api/types";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Icon } from "../components/ui/Icon";
import {
  INTEREST_LABELS,
  PROFILE_BLURBS,
  PROFILE_LABELS,
  money,
  minutesAsHours,
} from "../lib/format";
import "./Discover.css";

const INTERESTS: Interest[] = [
  "culture",
  "food",
  "history",
  "nightlife",
  "nature",
  "architecture",
  "adventure",
  "romance",
  "shopping",
  "museums",
  "beaches",
  "family_friendly",
];

const PROFILES: ProfileName[] = ["CHEAPEST", "BEST_VALUE", "ADVENTURE"];

interface Props {
  onSearch: (request: TripSearchRequest) => void;
  initial?: Partial<TripSearchRequest>;
}

/**
 * The trip builder (Part 6).
 *
 * Explicitly not a form: each question is a card with one decision in it, and
 * they are all visible at once so the traveler can see how little is being
 * asked. The only required inputs are where you start, when, how long, how
 * many and how much — everything else has a defensible default, because the
 * product's promise is that you *don't* have to know where you want to go.
 */
export function Discover({ onSearch, initial }: Props) {
  const [origin, setOrigin] = useState(initial?.origin ?? "Köln");
  const [airports, setAirports] = useState<OriginAirport[]>([]);
  const [airportError, setAirportError] = useState<string | null>(null);
  const [dateFrom, setDateFrom] = useState(initial?.date_from ?? "2026-09-10");
  const [flexible, setFlexible] = useState(initial?.date_flexible ?? true);
  const [duration, setDuration] = useState(initial?.duration_days ?? 5);
  const [travelers, setTravelers] = useState(initial?.travelers ?? 2);
  const [budget, setBudget] = useState(initial?.budget ?? 450);
  const [profile, setProfile] = useState<ProfileName>(initial?.profile ?? "BEST_VALUE");
  const [mode, setMode] = useState<SearchMode>(initial?.search_mode ?? "SMART");
  const [interests, setInterests] = useState<Interest[]>(
    (initial?.interests as Interest[]) ?? ["culture", "history", "food"],
  );
  const [avoid, setAvoid] = useState((initial?.avoided_destinations ?? []).join(", "));
  const [prefer, setPrefer] = useState((initial?.preferred_destinations ?? []).join(", "));

  // Airports are fetched as the origin settles, so the traveler sees early
  // that Detoura counts the ride to the airport - the first visible sign that
  // this is not a flight search.
  useEffect(() => {
    if (!origin.trim()) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const response = await api.origins(origin.trim());
        if (!cancelled) {
          setAirports(response.airports);
          setAirportError(null);
        }
      } catch {
        if (!cancelled) {
          setAirports([]);
          setAirportError("We don't know that starting point yet.");
        }
      }
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [origin]);

  function toggleInterest(interest: Interest) {
    setInterests((current) =>
      current.includes(interest)
        ? current.filter((item) => item !== interest)
        : [...current, interest],
    );
  }

  function submit() {
    const start = new Date(dateFrom);
    const end = new Date(start);
    // The window is exactly the trip the traveller described. "Flexible" then
    // means the engine may pick any start date that still fits inside it -
    // which is what the hint under the toggle promises. Quietly padding the
    // window would search a different question from the one they typed.
    end.setDate(end.getDate() + duration);
    onSearch({
      origin: origin.trim(),
      date_from: dateFrom,
      date_to: end.toISOString().slice(0, 10),
      duration_days: duration,
      date_flexible: flexible,
      travelers,
      budget,
      profile,
      search_mode: mode,
      interests,
      preferred_destinations: splitList(prefer),
      avoided_destinations: splitList(avoid),
    });
  }

  const valid = origin.trim().length > 0 && budget > 0 && !airportError;

  return (
    <div className="discover">
      <div className="container">
        <header className="discover__header">
          <h1 className="h1">Where should we look?</h1>
          <p className="lead">
            Five answers and we'll do the rest. You don't need a destination.
          </p>
        </header>

        <div className="discover__grid">
          <Card className="discover__card discover__card--wide">
            <Label icon={Icon.location({ size: 16 })}>Where are you starting?</Label>
            <input
              className="discover__input discover__input--lg"
              value={origin}
              onChange={(event) => setOrigin(event.target.value)}
              placeholder="Cologne"
              aria-label="Starting city"
            />
            {airportError ? (
              <p className="discover__hint discover__hint--warn">{airportError}</p>
            ) : (
              airports.length > 0 && (
                <>
                  <p className="discover__hint">Nearby airports we'll consider</p>
                  <ul className="discover__airports">
                    {airports.map((airport) => (
                      <li key={airport.code} className="discover__airport">
                        <span className="discover__airport-code">{airport.code}</span>
                        {airport.transfer_price !== null && (
                          <span className="discover__airport-cost subtle numeric">
                            {money(airport.transfer_price)} ·{" "}
                            {minutesAsHours(airport.transfer_minutes ?? 0)}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )
            )}
          </Card>

          <Card className="discover__card">
            <Label icon={Icon.calendar({ size: 16 })}>When?</Label>
            <input
              type="date"
              className="discover__input"
              value={dateFrom}
              onChange={(event) => setDateFrom(event.target.value)}
              aria-label="Earliest departure date"
            />
            <div className="discover__toggle-row">
              <Toggle
                checked={flexible}
                onChange={setFlexible}
                label="My dates are flexible"
              />
            </div>
            <p className="discover__hint">
              {flexible
                ? "We'll try every start date that fits."
                : "We'll leave on this exact date."}
            </p>
          </Card>

          <Card className="discover__card">
            <Label icon={Icon.clock({ size: 16 })}>How long?</Label>
            <Stepper
              value={duration}
              min={2}
              max={14}
              onChange={setDuration}
              suffix={duration === 1 ? "day" : "days"}
            />
          </Card>

          <Card className="discover__card">
            <Label icon={Icon.people({ size: 16 })}>Who's travelling?</Label>
            <Stepper
              value={travelers}
              min={1}
              max={8}
              onChange={setTravelers}
              suffix={travelers === 1 ? "traveller" : "travellers"}
            />
          </Card>

          <Card className="discover__card discover__card--wide">
            <Label icon={Icon.wallet({ size: 16 })}>Budget</Label>
            <div className="discover__budget">
              <input
                type="range"
                min={150}
                max={2000}
                step={10}
                value={budget}
                onChange={(event) => setBudget(Number(event.target.value))}
                className="discover__slider"
                aria-label="Total budget"
              />
              <div className="discover__budget-value">
                <input
                  type="number"
                  className="discover__input discover__input--budget numeric"
                  value={budget}
                  min={50}
                  step={10}
                  onChange={(event) => setBudget(Number(event.target.value))}
                  aria-label="Budget amount"
                />
                <span className="subtle">total, for everyone</span>
              </div>
            </div>
          </Card>

          <Card className="discover__card discover__card--wide">
            <Label icon={Icon.sliders({ size: 16 })}>What kind of trip?</Label>
            <div className="discover__profiles">
              {PROFILES.map((name) => (
                <button
                  key={name}
                  type="button"
                  className={`discover__profile ${profile === name ? "is-on" : ""}`}
                  onClick={() => setProfile(name)}
                  aria-pressed={profile === name}
                >
                  <span className="discover__profile-name">{PROFILE_LABELS[name]}</span>
                  <span className="discover__profile-blurb">{PROFILE_BLURBS[name]}</span>
                </button>
              ))}
            </div>
          </Card>

          <Card className="discover__card discover__card--wide">
            <Label icon={Icon.heart({ size: 16 })}>What do you love?</Label>
            <div className="discover__chips">
              {INTERESTS.map((interest) => (
                <button
                  key={interest}
                  type="button"
                  className={`chip ${interests.includes(interest) ? "chip--on" : ""}`}
                  onClick={() => toggleInterest(interest)}
                  aria-pressed={interests.includes(interest)}
                >
                  {INTEREST_LABELS[interest]}
                </button>
              ))}
            </div>
          </Card>

          <details className="discover__advanced">
            <summary>Anywhere in particular? (optional)</summary>
            <div className="discover__advanced-body">
              <div className="discover__field">
                <label htmlFor="prefer">Somewhere you had in mind</label>
                <input
                  id="prefer"
                  className="discover__input"
                  value={prefer}
                  onChange={(event) => setPrefer(event.target.value)}
                  placeholder="Madrid"
                />
                <p className="discover__hint">
                  We'll compare everything we find against this.
                </p>
              </div>
              <div className="discover__field">
                <label htmlFor="avoid">Somewhere to skip</label>
                <input
                  id="avoid"
                  className="discover__input"
                  value={avoid}
                  onChange={(event) => setAvoid(event.target.value)}
                  placeholder="Paris"
                />
              </div>
            </div>
          </details>
        </div>

        <div className="discover__submit">
          <div className="discover__modes" role="radiogroup" aria-label="Search depth">
            {(["QUICK", "SMART", "DEEP"] as SearchMode[]).map((option) => (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={mode === option}
                className={`discover__mode ${mode === option ? "is-on" : ""}`}
                onClick={() => setMode(option)}
              >
                {option === "QUICK" ? "Quick" : option === "SMART" ? "Smart" : "Deep"}
              </button>
            ))}
          </div>
          <Button
            size="lg"
            onClick={submit}
            disabled={!valid}
            icon={Icon.search({ size: 18 })}
          >
            Discover my trip
          </Button>
        </div>
      </div>
    </div>
  );
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function Label({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="discover__label">
      <span className="discover__label-icon">{icon}</span>
      <span>{children}</span>
    </div>
  );
}

function Stepper({
  value,
  min,
  max,
  onChange,
  suffix,
}: {
  value: number;
  min: number;
  max: number;
  onChange: (next: number) => void;
  suffix: string;
}) {
  return (
    <div className="stepper">
      <button
        type="button"
        onClick={() => onChange(Math.max(min, value - 1))}
        disabled={value <= min}
        aria-label="Decrease"
      >
        −
      </button>
      <span className="stepper__value">
        <span className="numeric">{value}</span> <span className="subtle">{suffix}</span>
      </span>
      <button
        type="button"
        onClick={() => onChange(Math.min(max, value + 1))}
        disabled={value >= max}
        aria-label="Increase"
      >
        +
      </button>
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <label className="toggle">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="toggle__track" aria-hidden="true">
        <span className="toggle__thumb" />
      </span>
      <span>{label}</span>
    </label>
  );
}

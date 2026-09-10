import { useState } from "react";
import { api } from "../api/client";
import { DetouraApiError } from "../api/types";
import type {
  ReoptimizeResponse,
  TripEditOperation,
  TripRecommendation,
  TripSearchRequest,
} from "../api/types";
import { Button } from "../components/ui/Button";
import { money } from "../lib/format";
import "./JourneyEditor.css";

type CityChoice = "open" | "keep" | "remove" | "replace";

function hoursLabel(h: number): string {
  const whole = Math.floor(h);
  const mins = Math.round((h - whole) * 60);
  return mins ? `${whole}h ${mins}m` : `${whole}h`;
}

function selectedTripPayload(trip: TripRecommendation) {
  const doorToDoor = Math.round(trip.travel_hours * 60);
  return {
    cities: trip.cities,
    origin_airport: trip.origin_airport,
    return_airport: trip.return_airport,
    departure: trip.departure,
    arrival: trip.arrival,
    duration_days: trip.duration_days,
    total_price: trip.total_price,
    intercity_minutes: Math.max(0, doorToDoor - trip.transfer_minutes),
    transfer_minutes: trip.transfer_minutes,
    usable_minutes: Math.round(trip.usable_hours * 60),
    experience_score: trip.experience_score,
    preference_match: trip.preference_match,
    accommodation_score: trip.accommodation_score,
    legs: trip.legs.map((l) => ({
      from: l.from,
      to: l.to,
      departure: l.departure,
      operator: l.operator ?? "",
      price_per_person: l.price_per_person,
    })),
    stays: trip.stays.map((s) => ({
      city: s.city,
      arrival: s.arrival,
      departure: s.departure,
      cost: s.cost,
      name: s.name ?? null,
    })),
  };
}

export function JourneyEditor({
  trip,
  searchRequest,
  originCity,
  onCancel,
  onAccept,
}: {
  trip: TripRecommendation;
  searchRequest: TripSearchRequest;
  originCity?: string;
  onCancel: () => void;
  onAccept: (next: TripRecommendation) => void;
}) {
  const [choices, setChoices] = useState<Record<string, CityChoice>>({});
  const [replacements, setReplacements] = useState<Record<string, string>>({});
  const [sheetCity, setSheetCity] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [empty, setEmpty] = useState(false);
  const [result, setResult] = useState<ReoptimizeResponse | null>(null);

  const kept = trip.cities.filter((c) => choices[c] === "keep");
  const removed = trip.cities.filter((c) => choices[c] === "remove");
  const replaced = trip.cities.filter((c) => choices[c] === "replace");
  const hasEdits = kept.length + removed.length + replaced.length > 0;
  // Removing every city would ask for a trip to nowhere; a single-city trip
  // can only be replaced.
  const wouldEmpty = removed.length >= trip.cities.length && replaced.length === 0;
  const canReoptimize = (removed.length + replaced.length > 0) && !wouldEmpty;
  const removeDisabled = trip.cities.length === 1;

  const setChoice = (city: string, choice: CityChoice) => {
    setChoices((c) => ({ ...c, [city]: choice }));
    setSheetCity(null);
  };

  const buildOps = (): TripEditOperation[] => {
    const ops: TripEditOperation[] = [];
    for (const city of trip.cities) {
      const ch = choices[city];
      if (ch === "keep") ops.push({ op: "lock_city", city });
      else if (ch === "remove") ops.push({ op: "remove_city", city });
      else if (ch === "replace")
        ops.push({
          op: "replace_city",
          city,
          replacement: replacements[city]?.trim() || null,
        });
    }
    return ops;
  };

  const reoptimize = async () => {
    setBusy(true);
    setError(null);
    setEmpty(false);
    try {
      const res = await api.reoptimize({
        trip_id: trip.id,
        search: searchRequest,
        trip: selectedTripPayload(trip),
        patch: { operations: buildOps() },
      });
      if (!res.trip) {
        setEmpty(true);
      } else {
        setResult(res);
      }
    } catch (e) {
      setError(
        e instanceof DetouraApiError
          ? e.message
          : "That combination could not be re-optimized.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (result?.trip) {
    return (
      <Comparison
        original={trip}
        originCity={originCity}
        result={result}
        onKeepOriginal={onCancel}
        onAccept={() => onAccept(result.trip!)}
      />
    );
  }

  return (
    <div className="jed">
      <div className="jed__head">
        <div>
          <span className="eyebrow">Edit journey</span>
          <h2>Tell Detoura what to keep</h2>
          <p className="muted">
            Keep the stops you love and remove the ones you don’t. Detoura keeps
            the kept cities as hard constraints and searches for a better
            version of the rest.
          </p>
        </div>
        <button className="jed__close" onClick={onCancel} aria-label="Close editor">
          ✕
        </button>
      </div>

      <ol className="jed__cities">
        {originCity && (
          <li className="jed__city jed__city--fixed">
            <span className="jed__city-name">{originCity}</span>
            <span className="jed__city-tag">Start &amp; end</span>
          </li>
        )}
        {trip.cities.map((city) => {
          const ch = choices[city] ?? "open";
          return (
            <li key={city} className={`jed__city jed__city--${ch}`}>
              <span className="jed__city-name">
                {ch === "keep" && <span aria-hidden>🔒 </span>}
                {city}
              </span>

              {ch === "keep" && <span className="jed__city-tag is-keep">Keep this stop</span>}
              {ch === "remove" && <span className="jed__city-tag is-remove">Remove stop</span>}
              {ch === "replace" && (
                <span className="jed__city-tag is-replace">Swap this stop</span>
              )}

              <div className="jed__city-actions">
                {(["keep", "remove", "replace"] as CityChoice[]).map((opt) => (
                  <button
                    key={opt}
                    className={`jed__btn${ch === opt ? " is-on" : ""}`}
                    disabled={opt === "remove" && removeDisabled}
                    title={
                      opt === "remove" && removeDisabled
                        ? "Removing the only stop would leave an empty trip — use Replace"
                        : undefined
                    }
                    onClick={() => setChoice(city, ch === opt ? "open" : opt)}
                  >
                    {opt === "keep" ? "Keep" : opt === "remove" ? "Remove" : "Replace"}
                  </button>
                ))}
              </div>
              <button
                className="jed__city-more"
                onClick={() => setSheetCity(city)}
                aria-label={`Options for ${city}`}
              >
                Options
              </button>

              {ch === "replace" && (
                <input
                  className="jed__replace-input"
                  placeholder="Replace with… (optional — Detoura chooses if blank)"
                  value={replacements[city] ?? ""}
                  onChange={(e) =>
                    setReplacements((r) => ({ ...r, [city]: e.target.value }))
                  }
                />
              )}
            </li>
          );
        })}
      </ol>

      {removeDisabled && (
        <p className="jed__hint muted">
          This journey has a single stop, so it can’t be removed — that would
          leave an empty trip. Use <b>Replace</b> to swap it for somewhere else.
        </p>
      )}

      {hasEdits && (
        <div className="jed__summary">
          {kept.length > 0 && (
            <div>
              <b>Keeping</b>
              <span>{kept.join(", ")}</span>
            </div>
          )}
          {removed.length > 0 && (
            <div>
              <b>Removing</b>
              <span>{removed.join(", ")}</span>
            </div>
          )}
          {replaced.length > 0 && (
            <div>
              <b>Swapping</b>
              <span>
                {replaced
                  .map((c) =>
                    replacements[c]?.trim() ? `${c} → ${replacements[c].trim()}` : c,
                  )
                  .join(", ")}
              </span>
            </div>
          )}
          <div>
            <b>Searching for</b>
            <span>A better alternative for the rest of the journey</span>
          </div>
        </div>
      )}

      {empty && (
        <p className="jed__note is-bad">
          Nothing satisfied that combination. Try removing fewer stops, or don’t
          name a specific replacement.
        </p>
      )}
      {error && <p className="jed__note is-bad">{error}</p>}

      <div className="jed__actions">
        <Button variant="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button
          size="lg"
          onClick={reoptimize}
          disabled={busy || !canReoptimize}
        >
          {busy ? "Re-optimizing…" : "Re-optimize journey"}
        </Button>
      </div>
      {!canReoptimize && hasEdits && (
        <p className="jed__hint muted">
          Remove or swap at least one stop to give Detoura something to
          re-optimize.
        </p>
      )}

      {sheetCity && (
        <div className="jed__sheet" role="dialog" aria-label={`Options for ${sheetCity}`}>
          <div className="jed__sheet-backdrop" onClick={() => setSheetCity(null)} />
          <div className="jed__sheet-panel">
            <h3>{sheetCity}</h3>
            <button onClick={() => setChoice(sheetCity, "keep")}>Keep this city</button>
            <button onClick={() => setChoice(sheetCity, "replace")}>Replace this city</button>
            <button onClick={() => setChoice(sheetCity, "remove")}>Remove this city</button>
            <button onClick={() => setChoice(sheetCity, "open")}>Leave it to Detoura</button>
            <button className="jed__sheet-cancel" onClick={() => setSheetCity(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Comparison({
  original,
  originCity,
  result,
  onKeepOriginal,
  onAccept,
}: {
  original: TripRecommendation;
  originCity?: string;
  result: ReoptimizeResponse;
  onKeepOriginal: () => void;
  onAccept: () => void;
}) {
  const next = result.trip!;
  const diff = result.diff;
  const routeOf = (t: TripRecommendation) =>
    [originCity, ...t.cities, originCity].filter(Boolean).join(" → ");

  return (
    <div className="jed">
      <span className="eyebrow">Compare</span>
      <h2>Original vs. your re-optimized journey</h2>
      {(result.locked.length > 0 || result.excluded.length > 0) && (
        <p className="muted jed__constraint">
          {result.locked.length > 0 && (
            <>Kept in the journey: <b>{result.locked.join(", ")}</b>. </>
          )}
          {result.excluded.length > 0 && (
            <>Removed: <b>{result.excluded.join(", ")}</b>.</>
          )}
        </p>
      )}

      <div className="jed__compare">
        <div className="jed__opt">
          <span className="jed__opt-label">Original</span>
          <p className="jed__opt-route">{routeOf(original)}</p>
          <dl>
            <div><dt>Price</dt><dd>{money(original.total_price, original.currency)}</dd></div>
            <div><dt>Travel time</dt><dd>{hoursLabel(original.travel_hours)}</dd></div>
            <div><dt>Usable time</dt><dd>{hoursLabel(original.usable_hours)}</dd></div>
          </dl>
        </div>
        <div className="jed__opt jed__opt--new">
          <span className="jed__opt-label">New option</span>
          <p className="jed__opt-route">{routeOf(next)}</p>
          <dl>
            <div><dt>Price</dt><dd>{money(next.total_price, next.currency)}</dd></div>
            <div><dt>Travel time</dt><dd>{hoursLabel(next.travel_hours)}</dd></div>
            <div><dt>Usable time</dt><dd>{hoursLabel(next.usable_hours)}</dd></div>
          </dl>
        </div>
      </div>

      {diff && (
        <div className="jed__verdict">
          {diff.improvements.length > 0 && (
            <div className="is-better">
              <b>Better</b>
              <ul>
                {diff.improvements.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}
          {diff.costs.length > 0 && (
            <div className="is-worse">
              <b>Trade-offs</b>
              <ul>
                {diff.costs.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}
          {diff.improvements.length === 0 && diff.costs.length === 0 && (
            <p className="muted">{diff.summary || "A similar journey with the same stops kept."}</p>
          )}
          {!diff.order_preserved && (
            <p className="muted jed__order-note">
              The order of the remaining stops may differ.
            </p>
          )}
        </div>
      )}

      {result.warnings.map((w) => (
        <p className="jed__note is-bad" key={w}>
          {w}
        </p>
      ))}

      <div className="jed__actions">
        <Button variant="secondary" onClick={onKeepOriginal}>
          Keep original
        </Button>
        <Button size="lg" onClick={onAccept}>
          Accept new journey
        </Button>
      </div>
      <p className="jed__hint muted">
        Accepting starts a fresh price check — the new journey is re-validated
        before you can book it.
      </p>
    </div>
  );
}

import type { NoResultsGuidance, TripSearchRequest } from "../../api/types";
import { money } from "../../lib/format";
import { Card } from "../ui/Card";
import "./States.css";

/**
 * Nothing matched (Part 24) — which is a real answer, and completely different
 * from a failed search.
 *
 * Every suggestion carries a patch from the backend, so "add €30" is a button
 * that re-runs the search rather than advice the traveler has to act on
 * themselves. The backend computed how far off the budget actually was; this
 * component just makes it clickable.
 */
export function NoResults({
  guidance,
  onRelax,
}: {
  guidance: NoResultsGuidance;
  onRelax: (patch: Partial<TripSearchRequest>) => void;
}) {
  return (
    <Card className="state">
      <div className="state__body">
        <h2 className="h2">We couldn't find an exact match.</h2>
        <p className="lead">{guidance.reason}</p>

        {guidance.closest_price !== null && (
          <div className="state__compare">
            <div>
              <div className="eyebrow">You asked for</div>
              <div className="state__figure numeric">
                {money(guidance.requested_budget)}
              </div>
            </div>
            <div>
              <div className="eyebrow">Closest we found</div>
              <div className="state__figure state__figure--accent numeric">
                {money(guidance.closest_price)}
              </div>
            </div>
          </div>
        )}

        {guidance.suggestions.length > 0 && (
          <>
            <div className="eyebrow state__suggestions-title">Try these changes</div>
            <div className="state__suggestions">
              {guidance.suggestions.map((suggestion) => (
                <button
                  key={suggestion.label}
                  type="button"
                  className="state__suggestion"
                  onClick={() => onRelax(suggestion.patch)}
                >
                  <span className="state__suggestion-label">{suggestion.label}</span>
                  <span className="state__suggestion-body">{suggestion.description}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </Card>
  );
}

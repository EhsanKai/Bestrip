import { useEffect, useState } from "react";
import type { SearchMode } from "../../api/types";
import { Icon } from "../ui/Icon";
import "./SearchProgress.css";

/**
 * The steps correspond to work the engine genuinely does, in the order it does
 * it: resolve origin airports, generate destination candidates, price
 * transport, book accommodation, measure usable time, score against
 * preferences, filter alternatives.
 *
 * Part 7 permits progress that does not map to exact backend operations, but
 * only if we do not pretend otherwise. It is cheaper to be truthful: these are
 * the real stages, so the list is honest even though the *timing* is a
 * paced animation rather than a live feed. A search takes about a second and
 * streaming per-stage events would cost more than it tells anyone.
 */
const STEPS = [
  "Checking nearby airports",
  "Exploring destinations",
  "Comparing transport combinations",
  "Optimizing accommodation",
  "Measuring usable travel time",
  "Matching your interests",
  "Evaluating alternative routes",
] as const;

interface Props {
  mode: SearchMode;
  /** Set when the search has finished, so the last steps complete at once. */
  done?: boolean;
  resultCount?: number;
}

export function SearchProgress({ mode, done = false, resultCount }: Props) {
  const [reached, setReached] = useState(0);

  useEffect(() => {
    if (done) {
      setReached(STEPS.length);
      return;
    }
    // Deep searches take much longer, so the pacing stretches rather than
    // finishing early and leaving a static list on screen for ten seconds.
    const interval = mode === "DEEP" ? 900 : mode === "QUICK" ? 110 : 220;
    const timer = window.setInterval(() => {
      // Stops one short of the end: the last tick belongs to the real result,
      // so the UI never claims to have finished before it has.
      setReached((n) => Math.min(n + 1, STEPS.length - 1));
    }, interval);
    return () => window.clearInterval(timer);
  }, [mode, done]);

  return (
    <div className="progress" role="status" aria-live="polite">
      <div className="progress__headline">
        <span className="progress__sparkle" aria-hidden="true">
          {Icon.sparkles({ size: 22 })}
        </span>
        <h2 className="h2">
          {done && resultCount !== undefined
            ? `We found ${resultCount} ${resultCount === 1 ? "trip" : "trips"} worth considering.`
            : mode === "DEEP"
              ? "Searching deeper for less obvious alternatives…"
              : "Finding your detours…"}
        </h2>
      </div>

      <ol className="progress__steps">
        {STEPS.map((step, index) => {
          const state = index < reached ? "done" : index === reached ? "active" : "idle";
          return (
            <li key={step} className={`progress__step progress__step--${state}`}>
              <span className="progress__marker" aria-hidden="true">
                {state === "done" ? Icon.check({ size: 13 }) : <span className="progress__dot" />}
              </span>
              {step}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

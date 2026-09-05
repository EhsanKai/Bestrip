import type { SearchMode, TripSearchRequest } from "../../api/types";
import { ReportIssueButton } from "../support/ReportIssueButton";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import "./SlowSearchNotice.css";

/** DEEP's own estimate is 8-20s with a 25s hard ceiling (search_modes.py).
 *  This notice only appears past that ceiling, so a normal DEEP search is
 *  never mistaken for a hang - by the time this shows, DEEP itself agrees
 *  something is wrong. */
const NEXT_FASTER_MODE: Record<SearchMode, SearchMode | null> = {
  DEEP: "SMART",
  SMART: "QUICK",
  QUICK: null,
};

interface Props {
  mode: SearchMode;
  request: TripSearchRequest | null;
  onKeepWaiting: () => void;
  onTryFaster: (mode: SearchMode) => void;
  onCancel: () => void;
}

/**
 * Search timeout handling (V6 production hardening).
 *
 * The one thing this must never do is read as a generic "Error occurred" -
 * nothing has failed yet. The request is still in flight; it is just taking
 * longer than any mode's own estimate says it should. So the copy says
 * exactly that, and offers the three honest options: keep waiting (it may
 * still finish), drop to a faster mode, or give up and go back to editing
 * the search.
 */
export function SlowSearchNotice({
  mode,
  request,
  onKeepWaiting,
  onTryFaster,
  onCancel,
}: Props) {
  const faster = NEXT_FASTER_MODE[mode];

  return (
    <div className="slow-notice" role="status" aria-live="polite">
      <div className="slow-notice__row">
        <span className="slow-notice__icon" aria-hidden="true">
          {Icon.clock({ size: 20 })}
        </span>
        <div>
          <p className="slow-notice__title">This search is taking longer than usual.</p>
          <p className="slow-notice__body muted">
            {mode === "DEEP"
              ? "Deep searches normally finish within about 20 seconds. This one has run past that, which usually means a transport or accommodation provider is slow to respond - not that anything is broken."
              : `${mode === "SMART" ? "Smart" : "Quick"} searches normally finish in a few seconds. This is unusual - a provider is likely responding slowly.`}
          </p>
        </div>
      </div>

      <div className="slow-notice__actions">
        <Button size="sm" variant="secondary" onClick={onKeepWaiting}>
          Keep waiting
        </Button>
        {faster && (
          <Button size="sm" variant="secondary" onClick={() => onTryFaster(faster)}>
            Try a faster search instead
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel and edit search
        </Button>
      </div>

      <div className="slow-notice__report">
        <ReportIssueButton
          quiet
          context={{
            situation: "search taking longer than expected",
            search_mode: mode,
            request,
          }}
          summary="A search took much longer than expected."
        />
      </div>
    </div>
  );
}

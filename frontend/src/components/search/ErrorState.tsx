import type { TripSearchRequest } from "../../api/types";
import { DetouraApiError } from "../../api/types";
import { ReportIssueButton } from "../support/ReportIssueButton";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Icon } from "../ui/Icon";
import "./States.css";

/**
 * The search did not complete (Part 23).
 *
 * The one sentence this component exists to never say is "No trips found."
 * The search never ran, so nothing is known about whether trips exist — and
 * offering budget advice here would send the traveler to fix a problem they
 * do not have.
 *
 * `request` is optional and used only for the "Report this issue" affordance
 * below — it is not shown to the traveler, only bundled into the report so a
 * real bug report carries the search that triggered it instead of just the
 * error message.
 */
export function ErrorState({
  error,
  request,
  onRetry,
}: {
  error: DetouraApiError;
  request?: TripSearchRequest | null;
  onRetry: () => void;
}) {
  const retryable =
    error.issue?.retryable ?? (error.status === 0 || error.status >= 500);
  return (
    <Card className="state state--error">
      <div className="state__body">
        <span className="state__icon">{Icon.alert({ size: 26 })}</span>
        <h2 className="h2">We couldn't complete the search</h2>
        <p className="lead">{error.message}</p>
        {retryable && (
          <Button size="lg" onClick={onRetry}>
            Try again
          </Button>
        )}
        <ReportIssueButton
          summary={error.message}
          context={{
            status: error.status,
            issue: error.issue,
            request,
          }}
        />
      </div>
    </Card>
  );
}

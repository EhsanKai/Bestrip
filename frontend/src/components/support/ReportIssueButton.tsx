import { useState } from "react";
import { track } from "../../lib/analytics";
import { buildIssueReport, reportIssue, type ErrorContext } from "../../lib/errorTracking";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Icon } from "../ui/Icon";
import "./ReportIssueButton.css";

interface Props {
  /** One honest sentence describing what went wrong, for the report body. */
  summary: string;
  context?: ErrorContext;
  /** Renders as a bare text link instead of an outlined button, for use
   *  inside already-busy states like the slow-search notice. */
  quiet?: boolean;
}

/**
 * The "Report an issue" affordance (V6 production hardening).
 *
 * There is no backend endpoint to send this to, and adding one is out of
 * scope for the frontend - so this opens a small modal with a pre-filled
 * report the user can either email (a `mailto:` link, prefilled) or copy.
 * Both paths go through `reportIssue()` in `lib/errorTracking.ts`, which is
 * the one place that would change if a real backend showed up later: this
 * component never builds the report text itself, so swapping "open mailto"
 * for "POST to /api/v1/feedback" is a change to that one file.
 */
export function ReportIssueButton({ summary, context, quiet = false }: Props) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const report = open ? buildIssueReport({ summary, context }) : null;

  const submit = (method: "email" | "copy") => {
    reportIssue({ summary, context });
    track("feedback_submitted", { method, summary });
  };

  return (
    <>
      {quiet ? (
        <button type="button" className="report-issue-link" onClick={() => setOpen(true)}>
          Report this issue
        </button>
      ) : (
        <Button
          variant="ghost"
          size="sm"
          icon={Icon.alert({ size: 14 })}
          onClick={() => setOpen(true)}
        >
          Report this issue
        </Button>
      )}

      {open && report && (
        <div
          className="report-issue-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Report an issue"
          onClick={() => setOpen(false)}
        >
          <Card className="report-issue-modal" onClick={(event) => event.stopPropagation()}>
            <div className="report-issue-modal__head">
              <h2 className="h3">Report an issue</h2>
              <button
                type="button"
                className="report-issue-close"
                aria-label="Close"
                onClick={() => setOpen(false)}
              >
                {Icon.close({ size: 18 })}
              </button>
            </div>

            <p className="muted report-issue-modal__lead">
              This is what we'd send. Nothing is sent automatically - email it to us or
              copy it into your own message.
            </p>

            <pre className="report-issue-body">{report.body}</pre>

            <div className="report-issue-actions">
              {/* A native anchor, not <Button>: only a real <a href="mailto:...">
               * hands off to the user's mail client, and Button always renders
               * a <button>. Styled to match it exactly instead. */}
              <a
                className="btn btn--primary btn--sm"
                href={report.mailtoHref}
                onClick={() => submit("email")}
              >
                Email us
              </a>
              <Button
                size="sm"
                variant="secondary"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(report.body);
                    setCopied(true);
                    submit("copy");
                    window.setTimeout(() => setCopied(false), 2000);
                  } catch {
                    /* clipboard permission denied: the text is still selectable above */
                  }
                }}
              >
                {copied ? "Copied" : "Copy details"}
              </Button>
            </div>
          </Card>
        </div>
      )}
    </>
  );
}

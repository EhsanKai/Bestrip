/* Error instrumentation receives only categories and a narrow operation context.
 * User-requested support reports remain a separate application feature. */
import { classifyAnalyticsError, type ErrorCategory } from "./analytics";

export type ErrorContext = Record<string, unknown>;

export interface SafeErrorContext {
  operation?: "search" | "search_deeper" | "saved_recheck";
}

export interface ErrorTrackingConfig {
  /** Defaults to `import.meta.env.VITE_SENTRY_DSN`. */
  dsn?: string;
}

interface ErrorTrackingAdapter {
  captureException(category: ErrorCategory, context: SafeErrorContext): void;
}

/** Development-only, categorical diagnostics. No raw error objects. */
const consoleAdapter: ErrorTrackingAdapter = {
  captureException(error, context) {
    // eslint-disable-next-line no-console
    if (import.meta.env.DEV) console.error("[errorTracking]", error, context);
  },
};

let adapter: ErrorTrackingAdapter = consoleAdapter;
let initialized = false;

/** Call once, at startup (see `main.tsx`). Safe to call more than once. */
export function init(config: ErrorTrackingConfig = {}): void {
  if (initialized) return;
  initialized = true;

  const dsn = config.dsn ?? (import.meta.env.VITE_SENTRY_DSN as string | undefined);
  if (!dsn) return; // No DSN: stay on the console adapter. Zero dependency, zero network.

  // See the TODO above the module doc: no real adapter exists yet, so a
  // configured DSN gets a loud dev-time warning rather than a silent no-op.
  if (import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.warn(
      "[errorTracking] VITE_SENTRY_DSN is set, but no Sentry adapter is wired up yet " +
        "(see the TODO in src/lib/errorTracking.ts). Falling back to console logging.",
    );
  }
}

/** The one function every screen and error boundary calls. */
export function captureException(error: unknown, context?: SafeErrorContext): void {
  try {
    const safe: SafeErrorContext = {};
    if (context?.operation && ["search", "search_deeper", "saved_recheck"].includes(context.operation)) safe.operation = context.operation;
    adapter.captureException(classifyAnalyticsError(error), safe);
  } catch { /* Instrumentation must not replace the application failure. */ }
}

/* ------------------------------------------------------------------ */
/* User-initiated issue reports ("Report an issue" affordance)         */
/* ------------------------------------------------------------------ */

export interface IssueReportInput {
  /** One honest sentence: what the user was doing, what went wrong. */
  summary: string;
  context?: ErrorContext;
}

export interface IssueReport {
  subject: string;
  /** Plain text, safe to copy into an email or a support ticket. */
  body: string;
  mailtoHref: string;
}

// There is no support inbox wired up yet; this is the address a real one
// would replace. It only needs to change here - every caller goes through
// `reportIssue()`, not this constant.
const SUPPORT_EMAIL = "support@detoura.app";

export function buildIssueReport({ summary, context }: IssueReportInput): IssueReport {
  const timestamp = new Date().toISOString();
  const sections = [
    `What happened: ${summary}`,
    `When: ${timestamp}`,
    `Page: ${typeof window !== "undefined" ? window.location.href : "unknown"}`,
    context && Object.keys(context).length > 0
      ? `Details:\n${JSON.stringify(context, null, 2)}`
      : null,
  ].filter((section): section is string => Boolean(section));

  const subject = `Detoura issue report: ${summary.slice(0, 80)}`;
  const body = sections.join("\n\n");
  const mailtoHref = `mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;

  return { subject, body, mailtoHref };
}

/** Prepare the user-requested support report without forwarding it to telemetry. */
export function reportIssue(input: IssueReportInput): IssueReport {
  const report = buildIssueReport(input);
  // The user-visible report is not an error/analytics payload.
  return report;
}

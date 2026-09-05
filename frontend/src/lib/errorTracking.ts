/* A vendor-neutral error-reporting seam, on the same principle as
 * `analytics.ts`: screens call `captureException()`, never a vendor SDK, so
 * the app runs with zero error-tracking dependency and zero network calls
 * until someone deliberately configures one.
 *
 * There is no Sentry adapter here yet, on purpose. `@sentry/*` is not a
 * dependency of this project (check `package.json`), and adding it as a real,
 * wired-up integration without verifying it against a live DSN would mean
 * shipping a "half" integration - the kind that silently no-ops in a way
 * that is easy to mistake for "it's fine, nothing broke" and hard to notice
 * is a lie. `init()` below reads `VITE_SENTRY_DSN` and, if it is set, warns
 * once in dev and otherwise stays on the console adapter, so setting the
 * variable without also shipping the adapter fails loudly in development
 * instead of pretending to work.
 *
 * TODO(sentry-adapter): when `@sentry/browser` is added as a dependency, wire
 * a real adapter into `init()` behind a dynamic `import("@sentry/browser")` so
 * a build with `VITE_SENTRY_DSN` unset never pulls the SDK in - the same
 * lazy-load discipline `analytics.ts` uses for GA4/Plausible.
 */

export type ErrorContext = Record<string, unknown>;

export interface ErrorTrackingConfig {
  /** Defaults to `import.meta.env.VITE_SENTRY_DSN`. */
  dsn?: string;
}

interface ErrorTrackingAdapter {
  captureException(error: unknown, context?: ErrorContext): void;
}

/** Prints in every environment - unlike analytics, a swallowed error is a
 *  real cost during development even with no backend configured. */
const consoleAdapter: ErrorTrackingAdapter = {
  captureException(error, context) {
    // eslint-disable-next-line no-console
    console.error("[errorTracking]", error, context ?? {});
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
export function captureException(error: unknown, context?: ErrorContext): void {
  adapter.captureException(error, context);
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

/**
 * The single entry point the "Report an issue" UI calls. It both hands back
 * something the user can act on (an email, or text to copy) and records that
 * a report was filed through whatever error-tracking backend is configured -
 * so swapping the no-op/console backend for a real one later is a change to
 * this file alone, not to every call site that lets a user file a report.
 */
export function reportIssue(input: IssueReportInput): IssueReport {
  const report = buildIssueReport(input);
  captureException(new Error(`User-reported issue: ${input.summary}`), input.context);
  return report;
}

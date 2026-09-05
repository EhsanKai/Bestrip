/* A vendor-neutral analytics seam.
 *
 * Every screen calls `track()`, never a vendor SDK directly - the same
 * discipline `api/client.ts` applies to network calls. That keeps two
 * promises:
 *
 *  1. Local dev works with zero configuration: with no `VITE_ANALYTICS_PROVIDER`
 *     set, `track()` is a no-op (a console line in dev, nothing in prod) and no
 *     script tag, cookie or network request is ever created.
 *  2. Wiring a real provider is a config change, not a code change: set
 *     `VITE_ANALYTICS_PROVIDER` and the matching id, and the adapter below
 *     lazy-loads the vendor's script only then - never by default, and never
 *     for a build that leaves the variable unset.
 *
 * The event names this file's callers use (`search_started`,
 * `trip_saved`, ...) are listed once, in the callers, not here - this module
 * does not know or care what the product's events are.
 */

export type AnalyticsProps = Record<string, unknown>;

export interface AnalyticsConfig {
  /** Defaults to `import.meta.env.VITE_ANALYTICS_PROVIDER`. */
  provider?: string;
}

interface AnalyticsAdapter {
  track(event: string, props?: AnalyticsProps): void;
}

/** Logs in dev so a missing event is visible without a network tab; does
 *  nothing in production, which is the correct behaviour for "not configured". */
const noopAdapter: AnalyticsAdapter = {
  track(event, props) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.debug("[analytics:noop]", event, props ?? {});
    }
  },
};

let adapter: AnalyticsAdapter = noopAdapter;
let initialized = false;

/**
 * Call once, at startup (see `main.tsx`). Safe to call more than once - only
 * the first call has any effect, so a screen does not have to know whether
 * the app has already initialised analytics.
 */
export function init(config: AnalyticsConfig = {}): void {
  if (initialized) return;
  initialized = true;

  const provider = (
    config.provider ??
    (import.meta.env.VITE_ANALYTICS_PROVIDER as string | undefined) ??
    ""
  )
    .trim()
    .toLowerCase();

  // Unset, blank or explicitly "none" all mean the same thing: stay silent.
  if (!provider || provider === "none") return;

  if (provider === "ga4") {
    void loadGa4Adapter().then((loaded) => {
      adapter = loaded;
    });
  } else if (provider === "plausible") {
    void loadPlausibleAdapter().then((loaded) => {
      adapter = loaded;
    });
  } else if (import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.warn(
      `[analytics] Unknown VITE_ANALYTICS_PROVIDER "${provider}" - staying no-op.`,
    );
  }
}

/** The one function every screen calls. */
export function track(event: string, props?: AnalyticsProps): void {
  adapter.track(event, props);
}

/* --- adapters: each is imported dynamically and only when selected, so ---- */
/* --- neither vendor's code, nor its script tag, ships in a build that    -- */
/* --- never asks for it.                                                   -- */

async function loadGa4Adapter(): Promise<AnalyticsAdapter> {
  const measurementId = import.meta.env.VITE_GA4_MEASUREMENT_ID as
    | string
    | undefined;
  if (!measurementId) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.warn(
        "[analytics] VITE_ANALYTICS_PROVIDER=ga4 but VITE_GA4_MEASUREMENT_ID is unset - staying no-op.",
      );
    }
    return noopAdapter;
  }

  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.async = true;
    script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(measurementId)}`;
    script.onload = () => {
      const w = window as unknown as {
        dataLayer: unknown[];
        gtag: (...args: unknown[]) => void;
      };
      w.dataLayer = w.dataLayer || [];
      w.gtag = function gtag(...args: unknown[]) {
        w.dataLayer.push(args);
      };
      w.gtag("js", new Date());
      w.gtag("config", measurementId);
      resolve({
        track(event, props) {
          w.gtag("event", event, props ?? {});
        },
      });
    };
    // A blocked or failed script load must not throw - it just means no
    // analytics this session, same as if the provider were never configured.
    script.onerror = () => resolve(noopAdapter);
    document.head.appendChild(script);
  });
}

async function loadPlausibleAdapter(): Promise<AnalyticsAdapter> {
  const domain =
    (import.meta.env.VITE_PLAUSIBLE_DOMAIN as string | undefined) ||
    window.location.hostname;

  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.defer = true;
    script.dataset.domain = domain;
    // The "manual" build does not auto-track pageviews or auto-bind outbound
    // links; this app has no page navigations to track, so the manual script
    // plus an explicit `track()` call is the correct-sized integration.
    script.src = "https://plausible.io/js/script.manual.js";
    script.onload = () => {
      const w = window as unknown as {
        plausible?: (event: string, opts?: { props?: AnalyticsProps }) => void;
      };
      resolve({
        track(event, props) {
          w.plausible?.(event, props ? { props } : undefined);
        },
      });
    };
    script.onerror = () => resolve(noopAdapter);
    document.head.appendChild(script);
  });
}

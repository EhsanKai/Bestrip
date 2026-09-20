import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
const OpsApp = lazy(() => import("./ops/OpsApp").then(module => ({ default: module.OpsApp })));
import { init as initAnalytics } from "./lib/analytics";
import { init as initErrorTracking } from "./lib/errorTracking";
import { applyNoIndexSeo } from "./lib/seo";
import "./design/tokens.css";
import "./design/base.css";
import "./design/luxury.css";

// Both are no-ops with no env vars set - see .env.example. Called once, here,
// so no screen has to know whether analytics or error tracking exist.
initAnalytics();
initErrorTracking();

// The ops console is the same bundle served under /ops, with its own root
// component and its own layout. The consumer app never mounts there and vice
// versa.
const isOps = window.location.pathname.replace(/\/+$/, "").endsWith("/ops")
  || window.location.pathname.startsWith("/ops/");
const isConsumerHome =
  window.location.pathname === "/" || window.location.pathname === "";
const isKnownSpaPath = isConsumerHome || isOps;

if (isOps) {
  applyNoIndexSeo(
    "Detoura ops | Private operations",
    "Detoura operations screens are private application surfaces and are not intended for search indexing.",
  );
} else if (!isKnownSpaPath) {
  applyNoIndexSeo(
    "Page not found | Detoura",
    "This Detoura URL does not map to a public indexable page.",
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Suspense fallback={<div className="container app__state" role="status">Loading…</div>}>
      {!isKnownSpaPath ? (
        <main className="container app__state" aria-labelledby="not-found-title">
          <h1 id="not-found-title">Page not found</h1>
          <p>This URL is not a public Detoura page.</p>
          <a href="/">Go to Detoura home</a>
        </main>
      ) : isOps ? <OpsApp /> : <App />}
    </Suspense>
  </StrictMode>,
);

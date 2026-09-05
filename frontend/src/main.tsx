import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { init as initAnalytics } from "./lib/analytics";
import { init as initErrorTracking } from "./lib/errorTracking";
import "./design/tokens.css";
import "./design/base.css";

// Both are no-ops with no env vars set - see .env.example. Called once, here,
// so no screen has to know whether analytics or error tracking exist.
initAnalytics();
initErrorTracking();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

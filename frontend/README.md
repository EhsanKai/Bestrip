# Detoura — frontend

The web client for Detoura. React 19 + TypeScript + Vite, talking to the
FastAPI product API at `/api/v1`.

## Running it

The frontend is useless on its own — every screen renders real engine output,
there is no mock data anywhere in `src/`. Start the backend first:

```bash
# from the repository root
uvicorn detoura.api.app:app --port 8000
```

Then, in a second shell:

```bash
cd frontend
npm install
npm run dev
```

Vite serves on <http://localhost:5173> and proxies `/api` to `127.0.0.1:8000`
(see `vite.config.ts`), so there is nothing to configure and no CORS to fight.

```bash
npm run build      # type-check (tsc -b) then bundle
npm run lint       # oxlint
```

## How it is laid out

```
src/
  design/         tokens.css (the whole brand as custom properties) + base.css
  components/ui/  Button, Card, Badge, Score, Icon - the design system
  components/trip/    RouteLine, RecommendationCard, BaselineComparison,
                      RouteMap, Timeline
  components/search/  SearchProgress, ProviderIssues, NoResults, ErrorState
  components/shell/   Header, MobileNav
  screens/        Landing, Discover, Results, TripDetail, Compare, SavedTrips
  state/          useSearch, useSaved
  api/            types.ts (a hand-written mirror of the API contract) + client.ts
  lib/format.ts   money, hours, percent, and every user-facing label
```

Three conventions are worth knowing before editing anything:

**The API contract is the only thing this app knows about the optimizer.**
`src/api/types.ts` mirrors `detoura/api/contracts.py` and nothing else. There
are no beam widths, no Pareto ranks, no objective vectors and no learned
weights in this codebase. If a screen needs a new fact, it gets added to the
contract on the backend and assembled there — not derived here from internals.

**Colour is meaning, not decoration.** All of it comes from `tokens.css`. Coral
marks one thing: the choice being recommended or the element being acted on.
Sage marks value and confirmation, amber marks caution, red marks error. A
component that hardcodes a hex value is a bug.

**A failure is never an empty list.** `useSearch` has three outcomes, not two:
results, no results, and failure. "We couldn't find trips" is only ever shown
when the providers actually answered and the answer was empty — when they
failed, `ProviderIssues` or `ErrorState` says so in words. See
`components/search/`.

## Dark mode

Founded on Midnight Navy rather than inverted from the light theme: the
surfaces get lighter as they come forward, ivory becomes the text colour, and
coral is desaturated slightly so it does not glow. The toggle lives in the
header and persists to `localStorage`; with no stored choice the app follows
`prefers-color-scheme`.

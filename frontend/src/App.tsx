import { useCallback, useEffect, useState } from "react";
import type { ProfileName, TripRecommendation, TripSearchRequest } from "./api/types";
import { SearchProgress } from "./components/search/SearchProgress";
import { ErrorState } from "./components/search/ErrorState";
import { Header } from "./components/shell/Header";
import { MobileNav } from "./components/shell/MobileNav";
import { Compare } from "./screens/Compare";
import { Discover } from "./screens/Discover";
import { Landing } from "./screens/Landing";
import { Results } from "./screens/Results";
import { SavedTrips } from "./screens/SavedTrips";
import { TripDetail } from "./screens/TripDetail";
import { useSearch } from "./state/useSearch";
import { useSaved } from "./state/useSaved";
import "./App.css";

type Screen =
  | "landing"
  | "discover"
  | "searching"
  | "results"
  | "detail"
  | "compare"
  | "saved";

/**
 * The shell.
 *
 * Screen state is held here rather than in a router because the journey is
 * genuinely linear (landing → discover → search → results → detail) and every
 * step needs the search result that produced it. A URL router would have to
 * re-run the search on every back-navigation, which is the wrong trade for a
 * one-to-three-second search whose results are already in memory.
 */
export default function App() {
  const search = useSearch();
  const saved = useSaved();
  const [screen, setScreen] = useState<Screen>("landing");
  const [selected, setSelected] = useState<TripRecommendation | null>(null);
  const [comparing, setComparing] = useState<string[]>([]);

  // Keep the shell in step with the search: entering "searching" is a state
  // transition the hook owns, and this maps it onto a screen.
  useEffect(() => {
    if (search.status === "searching") setScreen("searching");
    else if (search.status === "done" && screen === "searching") setScreen("results");
    else if (search.status === "failed" && screen === "searching") setScreen("results");
  }, [search.status, screen]);

  const runSearch = useCallback(
    (request: TripSearchRequest) => {
      setSelected(null);
      setComparing([]);
      void search.run(request);
    },
    [search],
  );

  const changeProfile = useCallback(
    (profile: ProfileName) => {
      if (!search.request) return;
      void search.run({ ...search.request, profile });
    },
    [search],
  );

  const relax = useCallback(
    (patch: Partial<TripSearchRequest>) => {
      if (!search.request) return;
      void search.run({ ...search.request, ...patch });
    },
    [search],
  );

  const toggleCompare = useCallback((trip: TripRecommendation) => {
    setComparing((current) =>
      current.includes(trip.id)
        ? current.filter((id) => id !== trip.id)
        : // Four is the ceiling the spec sets, and it is also the point past
          // which side-by-side stops being readable.
          current.length >= 4
          ? current
          : [...current, trip.id],
    );
  }, []);

  const openTrip = useCallback((trip: TripRecommendation) => {
    setSelected(trip);
    setScreen("detail");
    window.scrollTo({ top: 0 });
  }, []);

  const comparedTrips = (search.response?.recommendations ?? []).filter((trip) =>
    comparing.includes(trip.id),
  );

  return (
    <div className="app">
      <Header
        onHome={() => setScreen("landing")}
        onDiscover={() => setScreen("discover")}
        onSaved={() => setScreen("saved")}
        savedCount={saved.trips.length}
        showSearchNav={Boolean(search.response)}
        onResults={() => setScreen("results")}
      />

      <main className="app__main">
        {screen === "landing" && <Landing onDiscover={() => setScreen("discover")} />}

        {screen === "discover" && (
          <Discover onSearch={runSearch} initial={search.request ?? undefined} />
        )}

        {screen === "searching" && (
          <div className="container">
            <SearchProgress mode={search.request?.search_mode ?? "SMART"} />
          </div>
        )}

        {screen === "results" && search.failure && (
          <div className="container app__state">
            <ErrorState
              error={search.failure}
              onRetry={() => search.request && runSearch(search.request)}
            />
          </div>
        )}

        {screen === "results" && search.response && search.request && (
          <Results
            request={search.request}
            response={search.response}
            saved={saved.ids}
            comparing={comparing}
            deeperPending={search.deeperPending}
            onOpen={openTrip}
            onSave={saved.toggle}
            onCompare={toggleCompare}
            onOpenCompare={() => setScreen("compare")}
            onSearchDeeper={() => void search.searchDeeper().catch(() => undefined)}
            onProfileChange={changeProfile}
            onRelax={relax}
            onEdit={() => setScreen("discover")}
          />
        )}

        {screen === "detail" && selected && (
          <TripDetail
            trip={selected}
            saved={saved.ids.includes(selected.id)}
            origin={search.response?.origin ?? search.request?.origin}
            onBack={() => setScreen("results")}
            onSave={saved.toggle}
          />
        )}

        {screen === "compare" && (
          <Compare
            trips={comparedTrips}
            onBack={() => setScreen("results")}
            onOpen={openTrip}
            onRemove={toggleCompare}
          />
        )}

        {screen === "saved" && (
          <SavedTrips
            trips={saved.trips}
            onOpen={openTrip}
            onRemove={saved.toggle}
            onDiscover={() => setScreen("discover")}
          />
        )}
      </main>

      <MobileNav
        screen={screen}
        savedCount={saved.trips.length}
        hasResults={Boolean(search.response)}
        onNavigate={(next) => setScreen(next as Screen)}
      />
    </div>
  );
}

import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type {
  ProfileName,
  SearchMode,
  TripRecommendation,
  TripSearchRequest,
} from "./api/types";
import { SearchProgress } from "./components/search/SearchProgress";
import { SlowSearchNotice } from "./components/search/SlowSearchNotice";
import { ErrorState } from "./components/search/ErrorState";
import { Header } from "./components/shell/Header";
import {
  JourneyDrawer,
  type JourneyDrawerStatus,
} from "./components/journey/JourneyDrawer";
import { MobileNav } from "./components/shell/MobileNav";
import { track } from "./lib/analytics";
import { funnel } from "./lib/funnel";
const Compare = lazy(() => import("./screens/Compare").then(module => ({ default: module.Compare })));
const Discover = lazy(() => import("./screens/Discover").then(module => ({ default: module.Discover })));
import { Landing } from "./screens/Landing";
const Results = lazy(() => import("./screens/Results").then(module => ({ default: module.Results })));
const SavedTrips = lazy(() => import("./screens/SavedTrips").then(module => ({ default: module.SavedTrips })));
const TripDetail = lazy(() => import("./screens/TripDetail").then(module => ({ default: module.TripDetail })));
const BookingExperience = lazy(() => import("./screens/BookingExperience").then(module => ({ default: module.BookingExperience })));
const Login = lazy(() => import("./screens/Login").then(module => ({ default: module.Login })));
const MyTrips = lazy(() => import("./screens/MyTrips").then(module => ({ default: module.MyTrips })));
import { useSearch } from "./state/useSearch";
import { useSaved } from "./state/useSaved";
import { useAccount } from "./state/useAccount";
import {
  loadJourneyDraft,
  makeJourneyDraft,
  saveJourneyDraft,
  toDrawerModel,
  type JourneyDraft,
} from "./state/journeyDraft";
import "./App.css";

type Screen =
  | "landing"
  | "discover"
  | "searching"
  | "results"
  | "detail"
  | "compare"
  | "saved"
  | "booking"
  | "login"
  | "myTrips";

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
  const account = useAccount();
  const [screen, setScreen] = useState<Screen>("landing");
  const [selected, setSelected] = useState<TripRecommendation | null>(null);
  const [comparing, setComparing] = useState<string[]>([]);
  const [journeyDrawerOpen, setJourneyDrawerOpen] = useState(false);
  const [journeyDraft, setJourneyDraft] = useState<JourneyDraft | null>(() => loadJourneyDraft());
  const journeyModel = journeyDraft ? toDrawerModel(journeyDraft) : null;
  const journeyTrip = journeyDraft?.trip ?? null;

  // Keep the shell in step with the search: entering "searching" is a state
  // transition the hook owns, and this maps it onto a screen.
  useEffect(() => {
    if (search.status === "searching") setScreen("searching");
    else if (search.status === "done" && screen === "searching") {
      setScreen("results");
      funnel("RESULT_VIEW", {
        props: { result_count: search.response?.recommendations.length ?? 0 },
      });
    } else if (search.status === "failed" && screen === "searching") setScreen("results");
  }, [search.status, screen, search.response]);

  const runSearch = useCallback(
    (request: TripSearchRequest) => {
      setSelected(null);
      setComparing([]);
      funnel("SEARCH", {
        props: {
          search_mode: request.search_mode ?? "SMART",
          profile: request.profile ?? "BEST_VALUE",
          repeat: Boolean(search.request),
        },
      });
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

  // Saving records the party size the trip was found for, because the
  // re-check needs it to ask about seats and rooms later.
  const toggleSaved = useCallback(
    (trip: TripRecommendation) =>
      saved.toggle(trip, search.request?.travelers ?? 2),
    [saved, search.request],
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
    track("result_viewed", { trip_id: trip.id, rank: trip.rank });
    funnel("TRIP_OPEN", { props: { rank: trip.rank } });
  }, []);

  const selectJourney = useCallback((trip: TripRecommendation) => {
    const draft = makeJourneyDraft(trip, search.request);
    setSelected(trip);
    setJourneyDraft(draft);
    saveJourneyDraft(draft);
    setJourneyDrawerOpen(true);
    track("journey_selected", { trip_id: trip.id, rank: trip.rank });
    funnel("JOURNEY_SELECT", { props: { rank: trip.rank } });
  }, [search.request]);

  const setJourneyStatus = useCallback((status: JourneyDrawerStatus) => {
    if (status === "empty") {
      setJourneyDraft(null);
      saveJourneyDraft(null);
    }
  }, []);

  const continueCheckout = useCallback(() => {
    setJourneyDrawerOpen(false);
    if (journeyTrip) {
      setSelected(journeyTrip);
      setScreen(journeyTrip.selection_id ? "booking" : "detail");
    }
  }, [journeyTrip]);

  const viewJourney = useCallback(() => {
    setJourneyDrawerOpen(false);
    if (journeyTrip) {
      setSelected(journeyTrip);
      setScreen("detail");
    }
  }, [journeyTrip]);

  const reviewJourneyChanges = useCallback(() => {
    setJourneyDrawerOpen(false);
    if (journeyTrip) {
      setSelected(journeyTrip);
      setScreen("detail");
    }
  }, [journeyTrip]);

  // Accepting a re-optimized journey replaces the selection. The old journey
  // is still in `search.response.recommendations` (Keep original just returns
  // there); the new one becomes the thing that gets booked, and because its id
  // differs, the booking flow remounts clean.
  const acceptReoptimized = useCallback((next: TripRecommendation) => {
    setComparing([]);
    setSelected(next);
    window.scrollTo({ top: 0 });
    track("result_viewed", { trip_id: next.id, rank: next.rank });
  }, []);

  const comparedTrips = (search.response?.recommendations ?? []).filter((trip) =>
    comparing.includes(trip.id),
  );

  return (
    <div className="app">
      <Header
        onHome={() => setScreen("landing")}
        onDiscover={() => setScreen("discover")}
        onSaved={() => setScreen("myTrips")}
        onJourney={() => setJourneyDrawerOpen(true)}
        onAccount={() => setScreen("login")}
        savedCount={0}
        showSearchNav={Boolean(search.response)}
        onResults={() => setScreen("results")}
        journeyExists={Boolean(journeyDraft)}
        journeySummary={journeyModel ? `Your Journey · ${journeyModel.estimatedTripTotal}` : null}
        accountLabel={account.status === "authenticated" ? "Account ✓" : "Account"}
      />

      <main className="app__main">
        <Suspense fallback={<div className="container app__state" role="status" aria-label="Loading your journey"><div className="screen-skeleton" /><div className="screen-skeleton screen-skeleton--short" /></div>}>
        {screen === "landing" && <Landing onDiscover={() => setScreen("discover")} />}

        {screen === "discover" && (
          <Discover onSearch={runSearch} initial={search.request ?? undefined} />
        )}

        {screen === "searching" && (
          <div className="container">
            <SearchProgress mode={search.request?.search_mode ?? "SMART"} />
            {search.slow && (
              <SlowSearchNotice
                mode={search.request?.search_mode ?? "SMART"}
                request={search.request}
                onKeepWaiting={search.dismissSlow}
                onTryFaster={(mode: SearchMode) =>
                  search.request && runSearch({ ...search.request, search_mode: mode })
                }
                onCancel={() => {
                  search.reset();
                  setScreen("discover");
                }}
              />
            )}
          </div>
        )}

        {screen === "results" && search.failure && (
          <div className="container app__state">
            <ErrorState
              error={search.failure}
              request={search.request}
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
            onSave={toggleSaved}
            onCompare={toggleCompare}
            onSelectJourney={selectJourney}
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
            searchRequest={search.request ?? undefined}
            onBack={() => setScreen("results")}
            onSave={toggleSaved}
            onBook={() => setScreen("booking")}
            onReoptimized={acceptReoptimized}
          />
        )}

        {screen === "booking" && selected && (
          <BookingExperience
            // Keyed by trip id: accepting a re-optimized journey swaps
            // `selected`, which fully remounts the booking flow so no stale
            // intent, offer or revalidation from the previous journey survives.
            key={selected.id}
            trip={selected}
            travelers={search.request?.travelers ?? 1}
            onBack={() => setScreen("results")}
            onViewDetails={() => setScreen("detail")}
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

        {screen === "login" && (
          <Login
            profile={account.profile}
            sessionStatus={account.status}
            sessionError={account.error}
            onLogin={({ email, password }) => account.login(email, password)}
            onSignup={({ email, password }) => account.register(email, password)}
            onLogout={account.logout}
          />
        )}

        {screen === "myTrips" && (
          <MyTrips
            accountStatus={account.status}
            onDiscover={() => setScreen("discover")}
            onLogin={() => setScreen("login")}
          />
        )}

        {screen === "saved" && (
          <SavedTrips
            trips={saved.trips}
            rechecks={saved.rechecks}
            onRecheck={saved.recheck}
            onOpen={openTrip}
            onRemove={saved.toggle}
            onDiscover={() => setScreen("discover")}
          />
        )}
        </Suspense>
      </main>

      <MobileNav
        screen={screen}
        savedCount={0}
        hasResults={Boolean(search.response)}
        hasJourney={Boolean(journeyDraft)}
        onNavigate={(next) => setScreen(next as Screen)}
        onJourney={() => setJourneyDrawerOpen(true)}
        onAccount={() => setScreen("login")}
      />

      <JourneyDrawer
        open={journeyDrawerOpen}
        journey={journeyModel}
        onClose={() => setJourneyDrawerOpen(false)}
        onContinueCheckout={continueCheckout}
        onReviewChanges={reviewJourneyChanges}
        onExploreJourneys={() => {
          setJourneyDrawerOpen(false);
          setScreen("discover");
        }}
        onViewJourney={viewJourney}
        onStatusChange={setJourneyStatus}
      />
    </div>
  );
}

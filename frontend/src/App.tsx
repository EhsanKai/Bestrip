import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import type {
  ProfileName,
  SearchMode,
  TripRecommendation,
  TripSearchRequest,
} from "./api/types";
import { DetouraApiError } from "./api/types";
import { api, googleAuthStartUrl } from "./api/client";
import { SearchProgress } from "./components/search/SearchProgress";
import { SlowSearchNotice } from "./components/search/SlowSearchNotice";
import { ErrorState } from "./components/search/ErrorState";
import { Header } from "./components/shell/Header";
import {
  JourneyDrawer,
  type JourneyDrawerStatus,
} from "./components/journey/JourneyDrawer";
import { MobileNav } from "./components/shell/MobileNav";
import { recommendationSource, track } from "./lib/analytics";
import { clearGoogleReturnParams, readGoogleReturnOutcome } from "./lib/googleAuthReturn";
import { applyNoIndexSeo, applyPublicHomeSeo } from "./lib/seo";
const Compare = lazy(() => import("./screens/Compare").then(module => ({ default: module.Compare })));
const Discover = lazy(() => import("./screens/Discover").then(module => ({ default: module.Discover })));
import { Landing } from "./screens/Landing";
const Results = lazy(() => import("./screens/Results").then(module => ({ default: module.Results })));
const SavedTrips = lazy(() => import("./screens/SavedTrips").then(module => ({ default: module.SavedTrips })));
const TripDetail = lazy(() => import("./screens/TripDetail").then(module => ({ default: module.TripDetail })));
const BookingExperience = lazy(() => import("./screens/BookingExperience").then(module => ({ default: module.BookingExperience })));
const Login = lazy(() => import("./screens/Login").then(module => ({ default: module.Login })));
import type { GoogleNotice } from "./screens/Login";
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
export default function App() {
  const search = useSearch();
  const saved = useSaved();
  const account = useAccount();
  const [screen, setScreen] = useState<Screen>("landing");
  const [selected, setSelected] = useState<TripRecommendation | null>(null);
  const [comparing, setComparing] = useState<string[]>([]);
  const [journeyDrawerOpen, setJourneyDrawerOpen] = useState(false);
  const [journeyDraft, setJourneyDraft] = useState<JourneyDraft | null>(() => loadJourneyDraft());
  useEffect(() => {
    if (!journeyDraft) return;
    let timer: ReturnType<typeof setTimeout>;
    const checkExpiry = () => {
      clearTimeout(timer);
      const remaining = journeyDraft.expiresAt - Date.now();
      if (remaining <= 0) {
        setJourneyDraft(null);
        loadJourneyDraft(); // Purge expired storage without removing a newer tab's valid draft.
      } else timer = setTimeout(checkExpiry, Math.min(remaining, 2_147_483_647));
    };
    checkExpiry();
    window.addEventListener("focus", checkExpiry);
    return () => { clearTimeout(timer); window.removeEventListener("focus", checkExpiry); };
  }, [journeyDraft]);
  const journeyModel = journeyDraft ? toDrawerModel(journeyDraft) : null;
  const journeyTrip = journeyDraft?.trip ?? null;
  // Google Sign-In: the backend owns the entire OAuth/OIDC exchange and
  // redirects the browser back to this same origin with plain, non-secret
  // query parameters (see lib/googleAuthReturn.ts) - never a token, code,
  // or anything this app treats as a credential. `googleLinkId` holds a
  // Case-C link ticket (V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md §4/§9)
  // between "backend said an existing account must verify itself first"
  // and "that verification (a normal password login) just succeeded".
  const [googleRedirecting, setGoogleRedirecting] = useState(false);
  const [googleNotice, setGoogleNotice] = useState<GoogleNotice | null>(null);
  const [googleJustSignedIn, setGoogleJustSignedIn] = useState(false);
  const [googleLinked, setGoogleLinked] = useState(false);
  const [googleLinkId, setGoogleLinkId] = useState<string | null>(null);
  const googleLinkAttempt = useRef(0);
  const previousAccount = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (account.status === "loading") return;
    const currentAccount = account.profile?.user_id ?? null;
    if (previousAccount.current !== undefined && previousAccount.current !== currentAccount) {
      if (previousAccount.current !== null) ++googleLinkAttempt.current;
      setGoogleLinkId(null);
      setGoogleNotice(null);
      setGoogleJustSignedIn(false);
      setGoogleLinked(false);
    }
    previousAccount.current = currentAccount;
  }, [account.status, account.profile?.user_id]);
  useEffect(() => {
    const outcome = readGoogleReturnOutcome(window.location.search);
    if (!outcome) return;
    clearGoogleReturnParams();
    setScreen("login");
    if (outcome.kind === "success") {
      setGoogleJustSignedIn(true);
    } else if (outcome.kind === "link_required") {
      setGoogleLinkId(outcome.linkId);
      setGoogleNotice(outcome);
    } else {
      setGoogleNotice(outcome);
    }
    // Reads the URL exactly once, on the initial page load this backend
    // redirect landed on - never re-run for later in-app screen changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const continueWithGoogle = useCallback(() => {
    if (googleRedirecting) return;
    setGoogleRedirecting(true);
    window.location.href = googleAuthStartUrl();
  }, [googleRedirecting]);
  const loginWithPassword = useCallback(async (payload: { email: string; password: string }) => {
    const attempt = ++googleLinkAttempt.current;
    await account.login(payload.email, payload.password);
    if (!googleLinkId || attempt !== googleLinkAttempt.current) return;
    const pendingLinkId = googleLinkId;
    setGoogleLinkId(null);
    try {
      await api.googleLinkConfirm({ link_id: pendingLinkId });
      if (attempt !== googleLinkAttempt.current) return;
      setGoogleNotice(null);
      setGoogleLinked(true);
    } catch (error) {
      // The password login above already succeeded and stands regardless -
      // a failed link (expired ticket, since-claimed by someone else) is
      // reported, never silently retried (§13) and never treated as a
      // login failure.
      if (attempt !== googleLinkAttempt.current) return;
      const status = error instanceof DetouraApiError ? error.status : 0;
      setGoogleNotice({ kind: "error", reason: status === 409 ? "link_conflict" : "failed" });
    }
  }, [account, googleLinkId]);
  useEffect(() => {
    if (screen === "landing") {
      track("landing_viewed", { landing_context: "home" }, { dedupeKey: "home" });
    }
  }, [screen]);
  // Keep the shell in step with the search: entering "searching" is a state
  // transition the hook owns, and this maps it onto a screen.
  useEffect(() => {
    if (search.status === "searching") setScreen("searching");
    else if (search.status === "done" && screen === "searching") {
      setScreen("results");
    } else if (search.status === "failed" && screen === "searching") setScreen("results");
  }, [search.status, screen, search.response]);
  useEffect(() => {
    if (screen === "landing") {
      applyPublicHomeSeo();
      return;
    }
    applyNoIndexSeo(
      "Detoura app | Private trip planning",
      "This Detoura application state is for trip planning, checkout, account, or booking use and is not intended for search indexing.",
    );
  }, [screen]);

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
    track("journey_viewed", {
      recommendation_rank: trip.rank,
      recommendation_source: recommendationSource(search.response?.diagnostics.supply_source),
      bookable: Boolean(trip.selection_id),
      leg_count: trip.legs.length,
      currency: trip.currency,
    });
  }, [search.response]);

  const selectJourney = useCallback((trip: TripRecommendation) => {
    const draft = makeJourneyDraft(trip, search.request);
    setSelected(trip);
    setJourneyDraft(draft);
    saveJourneyDraft(draft);
    setJourneyDrawerOpen(true);
    track("recommendation_selected", {
      recommendation_rank: trip.rank,
      recommendation_source: recommendationSource(search.response?.diagnostics.supply_source),
      bookable: Boolean(trip.selection_id),
      leg_count: trip.legs.length,
      currency: trip.currency,
    });
  }, [search.request, search.response]);

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
    track("journey_viewed", {
      recommendation_rank: next.rank,
      recommendation_source: recommendationSource(search.response?.diagnostics.supply_source),
      bookable: Boolean(next.selection_id),
      leg_count: next.legs.length,
      currency: next.currency,
    });
  }, [search.response]);

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
            // Keyed by account and trip: accepting a re-optimized journey swaps
            // `selected`, which fully remounts the booking flow so no stale
            // intent, offer or revalidation from the previous journey survives.
            key={`${account.profile?.user_id ?? "anonymous"}:${selected.id}`}
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
            key={account.profile?.user_id ?? "anonymous"}
            profile={account.profile}
            sessionStatus={account.status}
            sessionError={account.error}
            onDeleteAccount={account.deleteAccount}
            onLogin={loginWithPassword}
            onSignup={({ email, password }) => account.register(email, password)}
            onLogout={async () => {
              ++googleLinkAttempt.current;
              await account.logout();
              setGoogleLinkId(null);
              setGoogleNotice(null);
              setGoogleJustSignedIn(false);
              setGoogleLinked(false);
            }}
            onForgotPassword={async ({ email }) => {
              await api.requestPasswordReset({ email });
            }}
            onConfirmResetPassword={async ({ token, newPassword }) => {
              await api.confirmPasswordReset({ token, new_password: newPassword });
            }}
            onContinueWithGoogle={continueWithGoogle}
            googleRedirecting={googleRedirecting}
            googleNotice={googleNotice}
            googleJustSignedIn={googleJustSignedIn}
            googleLinked={googleLinked}
          />
        )}

        {screen === "myTrips" && (
          <MyTrips
            key={account.profile?.user_id ?? "anonymous"}
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

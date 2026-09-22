import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { DetouraApiError } from "../api/types";
import type {
  FinancialDocument,
  MyTripSummary,
  TripConfirmation,
} from "../api/types";
import type { AccountStatus } from "../state/useAccount";
import { track } from "../lib/analytics";
import { dayMonth, money } from "../lib/format";
import { stateFromTrip, routeText, documentLabel, documentTypeForAnalytics, humanize, type ConsumerState } from "../lib/myTripPresentation";
import "./MyTrips.css";

interface MyTripsProps {
  accountStatus: AccountStatus;
  onDiscover?: () => void;
  onLogin?: () => void;
}
type DetailState =
  | { status: "idle" | "loading"; error: null; trip: MyTripSummary | null; confirmation: TripConfirmation | null; documents: FinancialDocument[] }
  | { status: "ready"; error: null; trip: MyTripSummary; confirmation: TripConfirmation | null; documents: FinancialDocument[] }
  | { status: "error"; error: string; trip: MyTripSummary | null; confirmation: TripConfirmation | null; documents: FinancialDocument[] };

export function MyTrips({ accountStatus, onDiscover, onLogin }: MyTripsProps) {
  const viewScope = useRef({});
  const downloadsInFlight = useRef(new Map<string, AbortController>());
  useEffect(() => {
    const downloads = downloadsInFlight.current;
    return () => { downloads.forEach(controller => controller.abort()); downloads.clear(); };
  }, [accountStatus]);
  const [trips, setTrips] = useState<MyTripSummary[]>([]);
  const [listStatus, setListStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [listError, setListError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DetailState>({
    status: "idle",
    error: null,
    trip: null,
    confirmation: null,
    documents: [],
  });
  const [downloadState, setDownloadState] = useState<{
    id: string | null;
    error: string | null;
  }>({ id: null, error: null });
  const loadTrips = useCallback(async (signal?: AbortSignal) => {
    setListStatus("loading");
    setListError(null);
    try {
      const response = await api.listMyTrips(signal);
      if (signal?.aborted) return;
      setTrips(response.trips);
      setListStatus("ready");
      if (selectedId && !response.trips.some((trip) => trip.booking_id === selectedId)) {
        setSelectedId(null);
      }
    } catch (error) {
      if (signal?.aborted) return;
      setTrips([]);
      setListStatus("error");
      setListError(messageFor(error, "We could not load your trips."));
    }
  }, [selectedId]);

  useEffect(() => {
    if (accountStatus !== "authenticated") {
      // oxlint-disable-next-line react/set-state-in-effect
      setTrips([]);
      setSelectedId(null);
      setListStatus(accountStatus === "loading" ? "loading" : "idle");
      return;
    }
    const controller = new AbortController();
    void loadTrips(controller.signal);
    return () => controller.abort();
  }, [accountStatus, loadTrips]);

  useEffect(() => {
    if (!selectedId || accountStatus !== "authenticated") {
      // oxlint-disable-next-line react/set-state-in-effect
      setDetail({ status: "idle", error: null, trip: null, confirmation: null, documents: [] });
      setDownloadState({ id: null, error: null });
      return;
    }
    const controller = new AbortController();
    setDetail((current) => ({
      status: "loading",
      error: null,
      trip: current.trip?.booking_id === selectedId ? current.trip : null,
      confirmation: null,
      documents: [],
    }));
    void (async () => {
      try {
        const trip = await api.getMyTrip(selectedId, controller.signal);
        const [confirmationResult, documentsResult] = await Promise.allSettled([
          api.getTripConfirmation(selectedId, controller.signal),
          api.listTripDocuments(selectedId, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setDetail({
          status: "ready",
          error: null,
          trip,
          confirmation:
            confirmationResult.status === "fulfilled" ? confirmationResult.value : null,
          documents:
            documentsResult.status === "fulfilled" ? documentsResult.value.documents : [],
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        setDetail({
          status: "error",
          error: messageFor(error, "We could not load this trip."),
          trip: null,
          confirmation: null,
          documents: [],
        });
      }
    })();
    return () => controller.abort();
  }, [accountStatus, selectedId]);
  const sortedTrips = useMemo(
    () =>
      [...trips].sort(
        (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
      ),
    [trips],
  );
  useEffect(() => {
    if (accountStatus === "authenticated" && listStatus === "ready") {
      track("my_trips_viewed", {
        trip_count: sortedTrips.length,
        empty: sortedTrips.length === 0,
      }, { dedupeKey: "my_trips", dedupeScope: viewScope.current });
    }
  }, [accountStatus, listStatus, sortedTrips.length]);
  if (accountStatus === "loading") {
    return <StateShell title="Checking your account" body="We are restoring your Detoura session." />;
  }

  if (accountStatus !== "authenticated") {
    return (
      <StateShell
        title="Sign in to see your trips."
        body="My Trips is an account-owned view. Anonymous journeys are not shown here by the current backend contract."
        actionLabel="Sign in"
        onAction={onLogin}
        secondaryLabel="Discover a journey"
        onSecondary={onDiscover}
      />
    );
  }
  if (selectedId) {
    return (
      <TripDetailView
        detail={detail}
        downloadState={downloadState}
        onBack={() => setSelectedId(null)}
        onRetry={() => setSelectedId((id) => id)}
        onDownload={async (document) => {
          if (!selectedId || !document.download_available || downloadsInFlight.current.has(document.document_id)) return;
          const controller = new AbortController();
          downloadsInFlight.current.set(document.document_id, controller);
          setDownloadState({ id: document.document_id, error: null });
          try {
            const blob = await api.downloadTripDocument(selectedId, document.document_id, controller.signal);
            if (controller.signal.aborted) return;
            const url = URL.createObjectURL(blob);
            const link = window.document.createElement("a");
            link.href = url;
            link.download = `${document.document_number || document.document_id}.pdf`;
            link.rel = "noreferrer";
            window.document.body.append(link);
            link.click();
            link.remove();
            window.setTimeout(() => URL.revokeObjectURL(url), 0);
            setDownloadState({ id: null, error: null });
            track("document_downloaded", {
              document_type: documentTypeForAnalytics(document.document_type),
            });
          } catch (error) {
            if (controller.signal.aborted) return;
            setDownloadState({
              id: null,
              error: messageFor(error, "We could not download this document."),
            });
          } finally {
            if (downloadsInFlight.current.get(document.document_id) === controller) downloadsInFlight.current.delete(document.document_id);
          }
        }}
      />
    );
  }
  if (listStatus === "loading") {
    return <StateShell title="Loading your trips" body="We are asking Detoura for your account-owned bookings." />;
  }

  if (listStatus === "error") {
    return (
      <StateShell
        title="We could not load My Trips."
        body={listError ?? "The trip list is unavailable right now."}
        actionLabel="Try again"
        onAction={() => void loadTrips()}
      />
    );
  }
  if (sortedTrips.length === 0) {
    return <EmptyTrips onDiscover={onDiscover} />;
  }
  const featured = sortedTrips[0];
  const rest = sortedTrips.slice(1);
  return (
    <main className="my-trips" aria-labelledby="my-trips-title">
      <header className="my-trips__header">
        <div>
          <p className="my-trips__eyebrow">Your journeys with Detoura</p>
          <h1 id="my-trips-title">My Trips</h1>
          <p>Only bookings owned by your signed-in account appear here.</p>
        </div>
        <button className="my-trips__discover" type="button" onClick={onDiscover}>
          Discover a journey
        </button>
      </header>
      <section className="my-trips__section" aria-labelledby="next-journey-title">
        <div className="my-trips__section-head">
          <h2 id="next-journey-title">Latest trip</h2>
        </div>
        <TripCard trip={featured} featured onOpen={() => setSelectedId(featured.booking_id)} />
      </section>
      {rest.length > 0 && (
        <section className="my-trips__section" aria-labelledby="all-trips-title">
          <div className="my-trips__section-head">
            <h2 id="all-trips-title">All trips</h2>
            <span>{rest.length} more</span>
          </div>
          <div className="my-trips__grid">
            {rest.map((trip) => (
              <TripCard key={trip.booking_id} trip={trip} onOpen={() => setSelectedId(trip.booking_id)} />
            ))}
          </div>
        </section>
      )}
    </main>
  );
}

function TripCard({
  trip,
  featured = false,
  onOpen,
}: {
  trip: MyTripSummary;
  featured?: boolean;
  onOpen: () => void;
}) {
  const state = stateFromTrip(trip, null);
  const title = routeText(trip);
  return (
    <article
      className={featured ? "my-trips-feature" : "my-trip-card"}
      aria-labelledby={`${trip.booking_id}-title`}
    >
      <TripImage trip={trip} featured={featured} />
      <div className={featured ? "my-trips-feature__body" : "my-trip-card__body"}>
        <StatusBlock state={state} compact={!featured} />
        <p className="my-trips__reference">{trip.journey_reference}</p>
        <h3 id={`${trip.booking_id}-title`}>{title}</h3>
        <p>{state.detail}</p>
        <TripMeta trip={trip} />
        <div className={featured ? "my-trips-actions" : "my-trips-actions my-trips-actions--compact"}>
          <button className="my-trips-actions__primary" type="button" onClick={onOpen}>
            View trip
          </button>
        </div>
      </div>
    </article>
  );
}

function TripDetailView({
  detail,
  downloadState,
  onBack,
  onRetry,
  onDownload,
}: {
  detail: DetailState;
  downloadState: { id: string | null; error: string | null };
  onBack: () => void;
  onRetry: () => void;
  onDownload: (document: FinancialDocument) => Promise<void>;
}) {
  if (detail.status === "loading") {
    return (
      <main className="my-trips" aria-labelledby="trip-loading-title">
        <button className="my-trips__back" type="button" onClick={onBack}>Back to My Trips</button>
        <StateShell title="Loading trip" body="We are checking the latest booking, confirmation, and document state." compact />
      </main>
    );
  }

  if (detail.status === "error") {
    return (
      <main className="my-trips" aria-labelledby="trip-error-title">
        <button className="my-trips__back" type="button" onClick={onBack}>Back to My Trips</button>
        <StateShell title="We could not open this trip." body={detail.error} actionLabel="Try again" onAction={onRetry} compact />
      </main>
    );
  }

  if (detail.status !== "ready") return null;

  const trip = detail.trip;
  const state = stateFromTrip(trip, detail.confirmation);
  const documents = [...detail.documents].sort(
    (a, b) => Date.parse(b.issued_at) - Date.parse(a.issued_at),
  );

  return (
    <main className="my-trips my-trips--detail" aria-labelledby="trip-detail-title">
      <button className="my-trips__back" type="button" onClick={onBack}>Back to My Trips</button>
      <header className="my-trips__header my-trips__header--detail">
        <div>
          <p className="my-trips__eyebrow">{trip.journey_reference}</p>
          <h1 id="trip-detail-title">{routeText(trip)}</h1>
          <p>{trip.trip_label || "Detoura journey"}</p>
        </div>
      </header>

      <section className="my-trips-detail">
        <div className="my-trips-detail__main">
          <StatusBlock state={state} />
          <section className="my-trips-panel" aria-labelledby="trip-summary-title">
            <h2 id="trip-summary-title">Journey summary</h2>
            <TripMeta trip={trip} />
          </section>

          <section className="my-trips-panel" aria-labelledby="trip-truth-title">
            <h2 id="trip-truth-title">Current booking truth</h2>
            <dl className="my-trips-facts">
              <div><dt>Booking phase</dt><dd>{humanize(trip.phase)}</dd></div>
              <div><dt>Confirmation</dt><dd>{detail.confirmation ? humanize(detail.confirmation.status) : "Not issued yet"}</dd></div>
              <div><dt>Service tier</dt><dd>{detail.confirmation ? humanize(detail.confirmation.service_tier) : "Not exposed"}</dd></div>
              <div><dt>Finalized</dt><dd>{detail.confirmation?.finalized_at ? dayMonth(detail.confirmation.finalized_at) : "Not finalized"}</dd></div>
            </dl>
            {state.key === "PAYMENT_UNKNOWN" && (
              <p className="my-trips-callout">
                Detoura is still verifying the outcome. Do not retry payment or
                booking from this screen.
              </p>
            )}
            {state.key === "RECOVERY_REQUIRED" && (
              <p className="my-trips-callout">
                This is not a normal confirmed trip. Detoura needs to resolve
                the booking or payment state before it can be treated as settled.
              </p>
            )}
          </section>
        </div>

        <aside className="my-trips-detail__side">
          <section className="my-trips-panel" aria-labelledby="documents-title">
            <h2 id="documents-title">Financial documents</h2>
            {downloadState.error && (
              <p className="my-trips-callout my-trips-callout--error" role="alert">
                {downloadState.error}
              </p>
            )}
            {documents.length === 0 ? (
              <p className="muted">No financial documents have been issued for this trip.</p>
            ) : (
              <ul className="my-trips-documents">
                {documents.map((document) => (
                  <li key={document.document_id}>
                    <div>
                      <strong>{documentLabel(document)}</strong>
                      <span>{dayMonth(document.issued_at)} · {money(document.customer_total, document.currency)}</span>
                    </div>
                    {document.download_available ? (
                      <button
                        type="button"
                        className="my-trips-documents__download"
                        disabled={downloadState.id === document.document_id}
                        onClick={() => void onDownload(document)}
                      >
                        {downloadState.id === document.document_id ? "Downloading..." : "Download PDF"}
                      </button>
                    ) : (
                      <span className="my-trips-documents__unavailable">Unavailable</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </section>
    </main>
  );
}

function StatusBlock({ state, compact = false }: { state: ConsumerState; compact?: boolean }) {
  return (
    <div className={compact ? "my-trips-status my-trips-status--compact" : "my-trips-status"} data-tone={state.tone}>
      <span>{state.label}</span>
      <p>{state.detail}</p>
      {state.actionRequired && <strong>Needs attention</strong>}
    </div>
  );
}

function TripImage({ trip, featured = false }: { trip: MyTripSummary; featured?: boolean }) {
  const title = routeText(trip);
  return (
    <div className={featured ? "my-trips-image my-trips-image--feature is-fallback" : "my-trips-image is-fallback"} aria-hidden="true">
      <span>{title}</span>
    </div>
  );
}

function TripMeta({ trip }: { trip: MyTripSummary }) {
  return (
    <dl className="my-trips-meta">
      <div><dt>Created</dt><dd>{dayMonth(trip.created_at)}</dd></div>
      <div><dt>Travelers</dt><dd>{trip.party_size}</dd></div>
      <div><dt>Payable total</dt><dd>{money(trip.customer_total, trip.currency)}</dd></div>
    </dl>
  );
}

function EmptyTrips({ onDiscover }: { onDiscover?: () => void }) {
  return (
    <StateShell
      title="You do not have any account trips yet."
      body="When a signed-in booking belongs to your account, its status and documents will appear here."
      actionLabel="Discover a journey"
      onAction={onDiscover}
    />
  );
}

function StateShell({
  title,
  body,
  actionLabel,
  onAction,
  secondaryLabel,
  onSecondary,
  compact = false,
}: {
  title: string;
  body: string | null;
  actionLabel?: string;
  onAction?: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
  compact?: boolean;
}) {
  return (
    <main className={compact ? "my-trips my-trips--state my-trips--state-compact" : "my-trips my-trips--state"} aria-labelledby="my-trips-state-title">
      <section className="my-trips-empty">
        <div className="my-trips-empty__mark" aria-hidden="true" />
        <p className="my-trips__eyebrow">Your journeys with Detoura</p>
        <h1 id="my-trips-state-title">{title}</h1>
        {body && <p>{body}</p>}
        <div className="my-trips-actions">
          {actionLabel && onAction && <button type="button" onClick={onAction}>{actionLabel}</button>}
          {secondaryLabel && onSecondary && <button type="button" onClick={onSecondary}>{secondaryLabel}</button>}
        </div>
      </section>
    </main>
  );
}

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof DetouraApiError) return error.message;
  return fallback;
}

import "./MyTrips.css";

type TripStatus = "confirmed" | "recoveryRequired" | "demo" | "sandboxBooked";

type TripAction = "viewJourney" | "travelPass" | "documents" | "resolveAction";

interface PresentationTrip {
  id: string;
  journeyReference: string;
  destinations: string[];
  dates: string;
  duration: string;
  travelerCount: number;
  status: TripStatus;
  environment: "production" | "demo" | "sandbox";
  image?: {
    src: string;
    alt: string;
  };
  travelPassAvailable: boolean;
  documentsAvailable: boolean;
  actionRequired: boolean;
  nextAction: TripAction;
  summary: string;
}

interface MyTripsProps {
  trips?: PresentationTrip[];
  onViewTrip?: (id: string) => void;
  onViewTravelPass?: (id: string) => void;
  onViewDocuments?: (id: string) => void;
  onResolveAction?: (id: string) => void;
  onDiscover?: () => void;
}

const presentationTrips: PresentationTrip[] = [
  {
    id: "trip-prague-vienna-budapest",
    journeyReference: "DTR-V9-7K4P2M",
    destinations: ["Prague", "Vienna", "Budapest"],
    dates: "12-19 Oct 2026",
    duration: "8 days",
    travelerCount: 2,
    status: "confirmed",
    environment: "production",
    travelPassAvailable: true,
    documentsAvailable: true,
    actionRequired: false,
    nextAction: "viewJourney",
    summary: "Tickets, stay notes, and city timing are ready to review.",
  },
  {
    id: "trip-lisbon-porto",
    journeyReference: "DTR-V9-2Q8H6L",
    destinations: ["Lisbon", "Porto"],
    dates: "4-8 Dec 2026",
    duration: "5 days",
    travelerCount: 2,
    status: "recoveryRequired",
    environment: "production",
    travelPassAvailable: false,
    documentsAvailable: false,
    actionRequired: true,
    nextAction: "resolveAction",
    summary: "Part of this journey needs review before it can be treated as booked.",
  },
  {
    id: "trip-copenhagen",
    journeyReference: "DTR-V9-DEMO",
    destinations: ["Copenhagen"],
    dates: "14-17 Feb 2027",
    duration: "4 days",
    travelerCount: 1,
    status: "demo",
    environment: "demo",
    travelPassAvailable: false,
    documentsAvailable: false,
    actionRequired: false,
    nextAction: "viewJourney",
    summary: "A local prototype journey for checking layout and status language.",
  },
  {
    id: "trip-sandbox-alps",
    journeyReference: "DTR-V9-SANDBOX",
    destinations: ["Zurich", "Lucerne", "Interlaken"],
    dates: "2-9 Apr 2027",
    duration: "8 days",
    travelerCount: 2,
    status: "sandboxBooked",
    environment: "sandbox",
    travelPassAvailable: true,
    documentsAvailable: false,
    actionRequired: false,
    nextAction: "travelPass",
    summary: "Sandbox booking data, useful for previews but not a live booking.",
  },
  {
    id: "trip-venice-verona",
    journeyReference: "DTR-V8-4M2V9A",
    destinations: ["Venice", "Verona"],
    dates: "May 2026",
    duration: "6 days",
    travelerCount: 2,
    status: "confirmed",
    environment: "production",
    travelPassAvailable: false,
    documentsAvailable: true,
    actionRequired: false,
    nextAction: "viewJourney",
    summary: "Past journey details remain available for reference.",
  },
];

const statusCopy: Record<TripStatus, { label: string; detail: string; tone: string }> = {
  confirmed: {
    label: "Confirmed",
    detail: "Your journey is booked.",
    tone: "good",
  },
  recoveryRequired: {
    label: "Recovery required",
    detail: "We're resolving an issue with part of this journey.",
    tone: "attention",
  },
  demo: {
    label: "Demo journey",
    detail: "Local prototype data. This is not a live booking.",
    tone: "demo",
  },
  sandboxBooked: {
    label: "Sandbox booking",
    detail: "Sandbox booking data. This is not a production journey.",
    tone: "sandbox",
  },
};

const actionLabels: Record<TripAction, string> = {
  viewJourney: "View journey",
  travelPass: "View Travel Pass",
  documents: "View documents",
  resolveAction: "Review booking status",
};

export function MyTrips({
  trips = presentationTrips,
  onViewTrip,
  onViewTravelPass,
  onViewDocuments,
  onResolveAction,
  onDiscover,
}: MyTripsProps) {
  const upcomingTrips = trips.filter(trip => trip.dates !== "May 2026");
  const pastTrips = trips.filter(trip => trip.dates === "May 2026");
  const featuredTrip = upcomingTrips[0];
  const otherUpcoming = upcomingTrips.slice(1);

  if (trips.length === 0) {
    return <EmptyTrips onDiscover={onDiscover} />;
  }

  return (
    <main className="my-trips" aria-labelledby="my-trips-title">
      <header className="my-trips__header">
        <div>
          <p className="my-trips__eyebrow">Your journeys with Detoura</p>
          <h1 id="my-trips-title">My Trips</h1>
          <p>Your journeys, tickets and travel details in one place.</p>
        </div>
        <button className="my-trips__discover" type="button" onClick={onDiscover}>
          Discover a journey
        </button>
      </header>

      {featuredTrip && (
        <section className="my-trips__section" aria-labelledby="next-journey-title">
          <div className="my-trips__section-head">
            <h2 id="next-journey-title">Next journey</h2>
          </div>
          <FeaturedTrip trip={featuredTrip} handlers={{ onViewTrip, onViewTravelPass, onViewDocuments, onResolveAction }} />
        </section>
      )}

      <section className="my-trips__section" aria-labelledby="upcoming-title">
        <div className="my-trips__section-head">
          <h2 id="upcoming-title">Upcoming trips</h2>
          <span>{otherUpcoming.length} journeys</span>
        </div>
        <div className="my-trips__grid">
          {otherUpcoming.map(trip => (
            <TripCard key={trip.id} trip={trip} handlers={{ onViewTrip, onViewTravelPass, onViewDocuments, onResolveAction }} />
          ))}
        </div>
      </section>

      <section className="my-trips__section" aria-labelledby="past-title">
        <div className="my-trips__section-head">
          <h2 id="past-title">Past trips</h2>
          <span>{pastTrips.length} journey</span>
        </div>
        <div className="my-trips__past-list">
          {pastTrips.map(trip => (
            <PastTrip key={trip.id} trip={trip} handlers={{ onViewTrip, onViewDocuments }} />
          ))}
        </div>
      </section>
    </main>
  );
}

function FeaturedTrip({ trip, handlers }: { trip: PresentationTrip; handlers: TripHandlers }) {
  const status = statusCopy[trip.status];

  return (
    <article className="my-trips-feature" aria-labelledby={`${trip.id}-title`}>
      <TripImage trip={trip} featured />
      <div className="my-trips-feature__body">
        <StatusBlock status={status} actionRequired={trip.actionRequired} />
        <p className="my-trips__reference">{trip.journeyReference}</p>
        <h3 id={`${trip.id}-title`}>{routeText(trip.destinations)}</h3>
        <TripActions trip={trip} handlers={handlers} />
        <p>{trip.summary}</p>
        <TripMeta trip={trip} />
      </div>
    </article>
  );
}

function TripCard({ trip, handlers }: { trip: PresentationTrip; handlers: TripHandlers }) {
  const status = statusCopy[trip.status];

  return (
    <article className="my-trip-card" aria-labelledby={`${trip.id}-title`}>
      <TripImage trip={trip} />
      <div className="my-trip-card__body">
        <StatusBlock status={status} actionRequired={trip.actionRequired} compact />
        <p className="my-trips__reference">{trip.journeyReference}</p>
        <h3 id={`${trip.id}-title`}>{routeText(trip.destinations)}</h3>
        <p>{trip.summary}</p>
        <TripMeta trip={trip} />
        <TripActions trip={trip} handlers={handlers} compact />
      </div>
    </article>
  );
}

function PastTrip({ trip, handlers }: { trip: PresentationTrip; handlers: Pick<TripHandlers, "onViewTrip" | "onViewDocuments"> }) {
  return (
    <article className="my-trips-past" aria-labelledby={`${trip.id}-title`}>
      <div>
        <p className="my-trips__reference">{trip.journeyReference}</p>
        <h3 id={`${trip.id}-title`}>{routeText(trip.destinations)}</h3>
        <p>{trip.dates} · {trip.duration}</p>
      </div>
      <div className="my-trips-past__actions">
        <button type="button" onClick={() => handlers.onViewTrip?.(trip.id)}>
          View journey
        </button>
        {trip.documentsAvailable && (
          <button type="button" onClick={() => handlers.onViewDocuments?.(trip.id)}>
            Documents
          </button>
        )}
      </div>
    </article>
  );
}

interface TripHandlers {
  onViewTrip?: (id: string) => void;
  onViewTravelPass?: (id: string) => void;
  onViewDocuments?: (id: string) => void;
  onResolveAction?: (id: string) => void;
}

function TripActions({ trip, handlers, compact = false }: { trip: PresentationTrip; handlers: TripHandlers; compact?: boolean }) {
  return (
    <div className={compact ? "my-trips-actions my-trips-actions--compact" : "my-trips-actions"}>
      <button className="my-trips-actions__primary" type="button" onClick={() => runAction(trip, handlers)}>
        {actionLabels[trip.nextAction]}
      </button>
      {trip.travelPassAvailable && trip.nextAction !== "travelPass" && (
        <button type="button" onClick={() => handlers.onViewTravelPass?.(trip.id)}>
          Travel Pass
        </button>
      )}
      {trip.documentsAvailable && trip.nextAction !== "documents" && (
        <button type="button" onClick={() => handlers.onViewDocuments?.(trip.id)}>
          Documents
        </button>
      )}
    </div>
  );
}

function runAction(trip: PresentationTrip, handlers: TripHandlers) {
  if (trip.nextAction === "travelPass") handlers.onViewTravelPass?.(trip.id);
  else if (trip.nextAction === "documents") handlers.onViewDocuments?.(trip.id);
  else if (trip.nextAction === "resolveAction") handlers.onResolveAction?.(trip.id);
  else handlers.onViewTrip?.(trip.id);
}

function TripImage({ trip, featured = false }: { trip: PresentationTrip; featured?: boolean }) {
  if (!trip.image) {
    return (
      <div className={featured ? "my-trips-image my-trips-image--feature is-fallback" : "my-trips-image is-fallback"} aria-hidden="true">
        <span>{trip.destinations[0]}</span>
      </div>
    );
  }

  return (
    <figure className={featured ? "my-trips-image my-trips-image--feature" : "my-trips-image"}>
      <img src={trip.image.src} alt={trip.image.alt} />
    </figure>
  );
}

function StatusBlock({ status, actionRequired, compact = false }: { status: { label: string; detail: string; tone: string }; actionRequired: boolean; compact?: boolean }) {
  return (
    <div className={compact ? "my-trips-status my-trips-status--compact" : "my-trips-status"} data-tone={status.tone}>
      <span>{status.label}</span>
      <p>{status.detail}</p>
      {actionRequired && <strong>Action needed</strong>}
    </div>
  );
}

function TripMeta({ trip }: { trip: PresentationTrip }) {
  return (
    <dl className="my-trips-meta">
      <div><dt>Dates</dt><dd>{trip.dates}</dd></div>
      <div><dt>Duration</dt><dd>{trip.duration}</dd></div>
      <div><dt>Travelers</dt><dd>{trip.travelerCount}</dd></div>
    </dl>
  );
}

function EmptyTrips({ onDiscover }: { onDiscover?: () => void }) {
  return (
    <main className="my-trips my-trips--empty" aria-labelledby="my-trips-empty-title">
      <section className="my-trips-empty">
        <div className="my-trips-empty__mark" aria-hidden="true" />
        <p className="my-trips__eyebrow">Your journeys with Detoura</p>
        <h1 id="my-trips-empty-title">You haven't created a journey yet.</h1>
        <p>
          When you save or book a Detoura journey, its tickets, Travel Pass
          and documents will live here.
        </p>
        <button type="button" onClick={onDiscover}>Discover a journey</button>
      </section>
    </main>
  );
}

function routeText(destinations: string[]) {
  return destinations.join(" -> ");
}

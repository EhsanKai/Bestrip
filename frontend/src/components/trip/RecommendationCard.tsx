import { useId, useRef, useState } from "react";
import cityPhotos from "../../data/cityPhotos.json";
import type { TripRecommendation } from "../../api/types";
import { cityCountLabel, hours, joinCities, money, percent } from "../../lib/format";
import {
  AvailabilityBadge,
  ConfidenceBadge,
  IntensityBadge,
  PriceFreshnessBadge,
} from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Icon } from "../ui/Icon";
import { RouteLine } from "./RouteLine";
import "./RecommendationCard.css";

interface Props {
  trip: TripRecommendation;
  saved?: boolean;
  selected?: boolean;
  comparing?: boolean;
  onOpen?: (trip: TripRecommendation) => void;
  onSave?: (trip: TripRecommendation) => void;
  onCompare?: (trip: TripRecommendation) => void;
  onSelectJourney?: (trip: TripRecommendation) => void;
}

export function RecommendationCard({
  trip,
  saved = false,
  selected = false,
  comparing = false,
  onOpen,
  onSave,
  onCompare,
  onSelectJourney,
}: Props) {
  const modes = trip.legs.map((leg) => leg.mode);
  const cities = [...new Set(trip.cities)];
  const [activeCity, setActiveCity] = useState<string | null>(null);
  const previewId = useId();
  const cityButtons = useRef<Record<string, HTMLButtonElement | null>>({});
  const activeMatch = trip.destination_matches?.find(match => match.city === activeCity);
  const providerBookable = Boolean(trip.selection_id);
  const closePreview = () => {
    if (activeCity) cityButtons.current[activeCity]?.focus();
    setActiveCity(null);
  };

  return (
    <Card
      as="article"
      interactive
      selected={selected}
      className="rec"
      onClick={() => onOpen?.(trip)}
      aria-label={`${joinCities(trip.cities)}, ${trip.duration_days.toFixed(0)} days, ${money(trip.total_price, trip.currency)} estimated total`}
    >
      <div className={`rec__hero ${activeCity ? "rec__hero--preview" : ""}`}
        onPointerLeave={event => { if (event.pointerType === "mouse") setActiveCity(null); }}
        onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); closePreview(); } }}
        onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setActiveCity(null); }}>
        <div className="rec__copy">
          <div className="rec__topline">
            <span className="rec__rank">{trip.rank === 1 ? "Detoura pick" : `Option ${trip.rank}`}</span>
            {trip.rank === 1 && <span className="rec__editorial-mark" aria-hidden="true">◆</span>}
          </div>

          <h3 className="rec__title">{joinCities(trip.cities)}</h3>
          <div className="rec__meta">
            <span>{Icon.calendar({ size: 14 })}{trip.duration_days.toFixed(0)} days</span>

            <span>{Icon.location({ size: 14 })}{cityCountLabel(cities.length)}</span>
            <span className="rec__inline-price">{Icon.wallet({ size: 14 })}{money(trip.total_price, trip.currency)}</span>
          </div>

          <RouteLine nodes={trip.route_nodes} modes={modes} cities={trip.cities} compact />

          <div className="rec__trustline" aria-label="Trip confidence and availability">
            <IntensityBadge band={trip.intensity_band} />
            <ConfidenceBadge level={trip.confidence.level} />
            <AvailabilityBadge status={trip.availability} />
            <PriceFreshnessBadge status={trip.price_freshness} />
          </div>

          {trip.why_we_like_it && (
            <div className="rec__reason">
              <div className="rec__reason-label">Why it stands out</div>
              <p>{trip.why_we_like_it}</p>
            </div>
          )}

          {trip.highlights.length > 0 && (
            <ul className="rec__highlights" aria-label="Highlights">
              {trip.highlights.slice(0, 3).map((highlight) => <li key={highlight}>{highlight}</li>)}
            </ul>
          )}
        </div>

        <div className={`rec__collage rec__collage--${cities.length > 3 ? "many" : cities.length}`}
          onClick={event => event.stopPropagation()}>
          <div className="rec__city-panels" role="group" aria-label="Preview destinations">
            {cities.map(city => <button type="button" className="rec__city-panel" key={city}
              ref={element => { cityButtons.current[city] = element; }}
              aria-label={`Preview ${city}`} aria-expanded={activeCity === city}
              aria-controls={activeCity === city ? previewId : undefined}
              onPointerEnter={event => { if (event.pointerType === "mouse") setActiveCity(city); }}
              onFocus={() => setActiveCity(city)} onClick={() => setActiveCity(city)}>
              <CityImage city={city} />
              <span className="rec__city-name">{city}</span>
            </button>)}
          </div>
          {activeCity && <div className="rec__city-preview" id={previewId} role="region" aria-label={`${activeCity} destination preview`}>
            <CityImage key={activeCity} city={activeCity} />
            <div className="rec__preview-caption">
              <h4>{activeCity}</h4>
              {activeMatch?.strengths.length ? <p>{activeMatch.strengths.slice(0, 3).map(value => value.replaceAll("_", " ")).join(" · ")}</p> : null}
            </div>
            <button type="button" className="rec__preview-open" onClick={() => onOpen?.(trip)} aria-label={`Explore ${joinCities(trip.cities)} trip`}>
              Explore trip {Icon.arrowRight({ size: 16 })}
            </button>
            <button type="button" className="rec__preview-close" onClick={closePreview} aria-label="Back to all cities">
              {Icon.close({ size: 18 })}
            </button>
          </div>}
          <PhotoCredits cities={activeCity ? [activeCity] : cities} />
        </div>
      </div>

      <div className="rec__metrics">
        <Metric label="Usable time" value={hours(trip.usable_hours)} />
        <Metric label="In transit" value={hours(trip.travel_hours)} />
        <Metric label="Experience" value={percent(trip.experience_score)} />
        <Metric label="Your interests" value={percent(trip.preference_match)} />
      </div>

      <div className="rec__footer">
        <div className="rec__pricing">
          <div className="rec__fare"><strong>{money(trip.total_price, trip.currency)}</strong><span>estimated total · {money(trip.price_per_person, trip.currency)} each</span></div>
          <div className="rec__costs">
          <span><small>Transport</small>{money(trip.costs.transport, trip.currency)}</span>
          <span><small>Rooms</small>{money(trip.costs.accommodation, trip.currency)}</span>
          <span><small>Transfers</small>{money(trip.costs.ground_transfer, trip.currency)}</span>
        </div>

          {trip.over_budget_by && trip.over_budget_by > 0 ? <p className="rec__over">{money(trip.over_budget_by, trip.currency)} over your preferred budget</p> : null}
        </div>

        {trip.tradeoff && <p className="rec__tradeoff">{trip.tradeoff}</p>}

        <div className="rec__actions" onClick={(event) => event.stopPropagation()}>
          <button
            type="button"
            className={`rec__compare ${comparing ? "is-on" : ""}`}
            onClick={() => onCompare?.(trip)}
            aria-pressed={comparing}
          >
            {Icon.compare({ size: 15 })}
            {comparing ? "Comparing" : "Compare"}
          </button>
          <Button onClick={() => onOpen?.(trip)} iconAfter={Icon.arrowRight({ size: 16 })}>
            Explore trip
          </Button>
          <button
            type="button"
            className={`rec__select-journey${providerBookable ? "" : " rec__select-journey--limited"}`}
            onClick={() => onSelectJourney?.(trip)}
            aria-describedby={providerBookable ? undefined : `${trip.id}-bookability`}
          >
            {Icon.route({ size: 15 })}
            Select journey
          </button>
          <button
            type="button"
            className={`rec__save ${saved ? "rec__save--on" : ""}`}
            onClick={() => onSave?.(trip)}
            aria-pressed={saved}
            aria-label={saved ? "Remove from saved" : "Save this trip"}
          >
            {saved ? Icon.heartFilled({ size: 18 }) : Icon.heart({ size: 18 })}
          </button>
        </div>
        {!providerBookable && (
          <p className="rec__bookability" id={`${trip.id}-bookability`}>
            Planning recommendation only. Checkout opens when live provider inventory is available.
          </p>
        )}
      </div>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rec__metric"><span>{label}</span><strong className="numeric">{value}</strong></div>;
}

const photos: Record<string, (typeof cityPhotos)["Munich"]> = cityPhotos;

function CityImage({ city }: { city: string }) {
  const [failed, setFailed] = useState(false);
  const photo = photos[city];
  if (!photo || failed) {
    return <span className="rec__image-fallback" role="img" aria-label={`${city} city photo unavailable`}>
      {Icon.location({ size: 28 })}
      <span>{city}</span>
      <small>City photo unavailable</small>
    </span>;
  }

  return <img
    className="rec__city-image"
    src={photo.src}
    alt={photo.alt || `${city} destination view`}
    width="960"
    height="600"
    loading="lazy"
    decoding="async"
    referrerPolicy="no-referrer"
    onError={() => setFailed(true)}
  />;
}

function PhotoCredits({ cities }: { cities: string[] }) {
  const available = cities.filter(city => photos[city]);
  if (!available.length) return null;
  return <details className="rec__photo-credit">
    <summary>Photo credits</summary>
    {available.map(city => <p key={city}>{city}: {photos[city].credit} · <a href={photos[city].source} target="_blank" rel="noreferrer">Wikimedia Commons</a> · <a href={photos[city].licenseUrl} target="_blank" rel="noreferrer">{photos[city].license}</a>. Cropped to fit.</p>)}
  </details>;
}

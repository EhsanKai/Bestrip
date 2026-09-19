"use client";
import { useState } from "react";
import cityPhotos from "../../data/cityPhotos.json";
import { Icon } from "../ui/Icon";
import "./CityGallery.css";

type CityPhoto = { src: string; alt: string; source: string; credit: string; license: string; licenseUrl: string };
const photos: Record<string, CityPhoto> = cityPhotos;

export function CityGallery({ cities }: { cities: string[] }) {
  const stops = [...new Set(cities)];
  return <section className="city-gallery" aria-label="Photos of your destinations">
    <div className="city-gallery__heading"><h2 className="h2">A glimpse of your journey</h2><p className="muted">The places along your route.</p></div>
    <div className={`city-gallery__grid ${stops.length === 1 ? "city-gallery__grid--single" : ""}`}>
      {stops.map(city => <CityPhotoCard city={city} key={city} />)}
    </div>
  </section>;
}
function CityPhotoCard({ city }: { city: string }) {
  const photo = photos[city];
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  return <figure className="city-gallery__item">
    <div className={`city-gallery__image ${loaded ? "is-loaded" : ""}`}>
      {photo && !failed ? <img src={photo.src} alt={photo.alt} width="960" height="600" loading="lazy" referrerPolicy="no-referrer" onLoad={() => setLoaded(true)} onError={() => setFailed(true)} />
        : <div className="city-gallery__fallback">{Icon.location({size:30})}<span>{city}</span><small>City photograph unavailable</small></div>}
    </div>
    <figcaption><h3>{city}</h3>{photo && <details><summary>Photo credit</summary><p>{photo.credit}. <a href={photo.source} target="_blank" rel="noreferrer">Wikimedia Commons</a>. {photo.licenseUrl ? <a href={photo.licenseUrl} target="_blank" rel="noreferrer">{photo.license}</a> : photo.license}. Display cropped to fit.</p></details>}</figcaption>
  </figure>;
}

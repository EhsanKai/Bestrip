import "./BrandLogo.css";

/** Outlined wordmark: the smiling D is both the departure and return point. */
export function BrandLogo() {
  return <span className="brand-logo" role="img" aria-label="Detoura">
    <img className="brand-logo__light" src="/brand/detoura-wordmark-navy.svg" alt="" width="262" height="68" />
    <img className="brand-logo__dark" src="/brand/detoura-wordmark-ivory.svg" alt="" width="262" height="68" />
  </span>;
}

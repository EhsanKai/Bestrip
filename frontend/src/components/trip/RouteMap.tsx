import { money } from "../../lib/format";
import "./RouteMap.css";

/**
 * Approximate positions on a stylised Western/Central Europe panel, in the
 * SVG's own 0-100 coordinate space rather than real latitude and longitude.
 *
 * This is deliberate. Part 5 asks for "an elegant European map" and Part 19
 * warns against looking like Google Maps: a true projection of eight cities
 * would put most of them in a cluster and waste the frame. These positions
 * keep the real *relative* geography — Madrid south-west, Budapest east,
 * Amsterdam north — while spacing the cities so the routes are legible. It is
 * a diagram of a journey, not a navigational aid, and it should not pretend
 * otherwise.
 */
const PLACES: Record<string, { x: number; y: number }> = {
  Cologne: { x: 44, y: 38 },
  Köln: { x: 44, y: 38 },
  Düsseldorf: { x: 42, y: 34 },
  Brussels: { x: 36, y: 40 },
  Amsterdam: { x: 39, y: 29 },
  London: { x: 27, y: 33 },
  Paris: { x: 33, y: 48 },
  Munich: { x: 53, y: 50 },
  Berlin: { x: 57, y: 28 },
  Prague: { x: 60, y: 40 },
  Vienna: { x: 64, y: 50 },
  Budapest: { x: 71, y: 54 },
  Zurich: { x: 46, y: 53 },
  Milan: { x: 49, y: 62 },
  Rome: { x: 56, y: 74 },
  Barcelona: { x: 30, y: 71 },
  Madrid: { x: 18, y: 72 },
  Lisbon: { x: 6, y: 76 },
};

export interface MapRoute {
  nodes: string[];
  price?: number;
  label?: string;
  highlight?: boolean;
}

interface Props {
  routes: MapRoute[];
  /** Draw the return leg back to the origin. */
  closed?: boolean;
  height?: number;
  compact?: boolean;
}

function position(name: string) {
  return PLACES[name] ?? { x: 50, y: 50 };
}

/** A gentle arc, so overlapping routes stay distinguishable. */
function arc(a: { x: number; y: number }, b: { x: number; y: number }, lift = 0.18) {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  // Perpendicular offset, scaled by the leg's own length so short hops bow
  // less than long ones.
  const cx = mx - dy * lift;
  const cy = my + dx * lift;
  return `M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`;
}

export function RouteMap({ routes, closed = false, height = 460, compact = false }: Props) {
  const shown = routes.map((route) =>
    closed && route.nodes.length > 1
      ? { ...route, nodes: [...route.nodes, route.nodes[0]] }
      : route,
  );

  // Fit the view to what is actually drawn. Without this a Cologne-Munich-
  // Vienna trip renders as three dots in the middle of an empty continent -
  // technically correct and visually useless. The panel is a diagram of *this*
  // journey, so it should be framed on this journey.
  const points = shown.flatMap((route) => route.nodes.map(position));
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const pad = 14;
  const minX = Math.min(...xs) - pad;
  const minY = Math.min(...ys) - pad;
  // A minimum span stops a two-city trip from zooming in so far that the
  // labels collide.
  const boxWidth = Math.max(Math.max(...xs) - Math.min(...xs) + pad * 2, 34);
  const boxHeight = Math.max(Math.max(...ys) - Math.min(...ys) + pad * 2, 26);
  const scale = 100 / Math.max(boxWidth, boxHeight);

  const labelled = new Map<string, { x: number; y: number; highlight: boolean }>();
  shown.forEach((route) => {
    route.nodes.forEach((node) => {
      const existing = labelled.get(node);
      labelled.set(node, {
        ...position(node),
        highlight: (existing?.highlight ?? false) || Boolean(route.highlight),
      });
    });
  });

  return (
    <figure className={`routemap ${compact ? "routemap--compact" : ""}`} style={{ height }}>
      <svg
        viewBox={`${minX} ${minY} ${boxWidth} ${boxHeight}`}
        preserveAspectRatio="xMidYMid meet"
        className="routemap__svg"
        role="img"
        aria-label={shown
          .map((r) => r.nodes.join(" to "))
          .join("; ")}
      >
        <defs>
          <pattern id="grid" width="5" height="5" patternUnits="userSpaceOnUse">
            <path d="M 5 0 L 0 0 0 5" fill="none" stroke="currentColor" strokeWidth="0.12" />
          </pattern>
          <filter id="softglow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="1.2" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* The cartographic texture: a graticule, not a basemap. */}
        <rect
          x={minX}
          y={minY}
          width={boxWidth}
          height={boxHeight}
          fill="url(#grid)"
          className="routemap__grid"
        />

        {shown.map((route, index) =>
          route.nodes.slice(0, -1).map((from, legIndex) => {
            const to = route.nodes[legIndex + 1];
            return (
              <path
                key={`${index}-${legIndex}`}
                // Alternate the bow so an out-and-back does not draw the
                // return leg exactly on top of the outbound one.
                d={arc(
                  position(from),
                  position(to),
                  (legIndex % 2 === 0 ? 0.16 : -0.16) + index * 0.04,
                )}
                className={`routemap__leg ${route.highlight ? "routemap__leg--hi" : ""}`}
                fill="none"
                // Stroke and type are in user units, so they must be divided
                // back out or a zoomed-in map draws fat lines and huge labels.
                strokeWidth={(route.highlight ? 0.75 : 0.45) / scale}
                style={{ animationDelay: `${(index * 3 + legIndex) * 140}ms` }}
              />
            );
          }),
        )}

        {[...labelled.entries()].map(([name, p]) => (
          <g key={name} className={`routemap__place ${p.highlight ? "routemap__place--hi" : ""}`}>
            <circle
              cx={p.x}
              cy={p.y}
              r={(p.highlight ? 1.5 : 1.1) / scale}
              filter="url(#softglow)"
            />
            <text
              x={p.x}
              y={p.y - 2.6 / scale}
              textAnchor="middle"
              className="routemap__name"
              fontSize={2.6 / scale}
            >
              {name}
            </text>
          </g>
        ))}
      </svg>

      {shown.some((route) => route.price !== undefined) && (
        <figcaption className="routemap__prices">
          {shown.map((route, index) =>
            route.price === undefined ? null : (
              <span
                key={index}
                className={`routemap__price ${route.highlight ? "routemap__price--hi" : ""}`}
              >
                <span className="numeric">{money(route.price)}</span>
                {route.label && <span className="routemap__price-label">{route.label}</span>}
              </span>
            ),
          )}
        </figcaption>
      )}
    </figure>
  );
}

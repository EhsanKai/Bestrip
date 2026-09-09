import { useId } from "react";
import {
  GROUND_Y,
  STAGE_H,
  STAGE_W,
  landmarkFor,
  type Landmark,
} from "./cityLandmarks";
import { variantFor, type IllustrationVariant } from "./illustrationVariant";
import "./CityIllustration.css";

interface Props {
  cities: string[];
  variant?: IllustrationVariant;
  /** Draw the coral detour line. Reserved for the cards worth pointing at. */
  detour?: boolean;
  className?: string;
}

/**
 * A city drawn as a line, not photographed.
 *
 * Two constraints shaped this. It must work with no network and no image
 * service, so every drawing is inline SVG built from a local registry. And it
 * must never become the loudest thing on a card it shares with a price and a
 * call to action, so it is stroke-only, low-contrast, and sits behind the
 * information rather than beside it.
 *
 * The composition is chosen from the city names rather than from the list
 * index. Index would have been simpler and wrong: re-sorting the results by
 * price would redraw every card, and a trip whose picture changes when you
 * sort it does not feel like a place. Keyed on the cities, Prague plus Vienna
 * is drawn the same way wherever it lands.
 */
export function CityIllustration({
  cities,
  variant,
  detour = false,
  className = "",
}: Props) {
  const chosen = variant ?? variantFor(cities);
  const primary = landmarkFor(cities[0] ?? "");
  const secondary = cities.length > 1 ? landmarkFor(cities[1]) : null;
  const uid = useId().replace(/:/g, "");

  return (
    <div
      className={`cityart cityart--${chosen} ${className}`}
      // Decorative: every fact it depicts is already in the card's text, so
      // announcing it would only make a screen reader repeat the route.
      aria-hidden="true"
    >
      <svg
        viewBox={`0 0 ${STAGE_W} ${STAGE_H}`}
        preserveAspectRatio="xMidYMax meet"
        className="cityart__svg"
        role="presentation"
      >
        <defs>
          {/* Fades the drawing out toward the card's edge so it reads as part
            * of the paper rather than as a picture pasted onto it. */}
          <linearGradient id={`fade-${uid}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="white" stopOpacity="0.55" />
            <stop offset="30%" stopColor="white" stopOpacity="1" />
            <stop offset="100%" stopColor="white" stopOpacity="1" />
          </linearGradient>
          <mask id={`mask-${uid}`}>
            <rect width={STAGE_W} height={STAGE_H} fill={`url(#fade-${uid})`} />
          </mask>
        </defs>

        <g mask={`url(#mask-${uid})`}>
          <Backdrop variant={chosen} />
          <Composition
            variant={chosen}
            primary={primary}
            secondary={secondary}
          />
          {detour && <DetourLine variant={chosen} />}
        </g>
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Composition                                                         */
/* ------------------------------------------------------------------ */

function Composition({
  variant,
  primary,
  secondary,
}: {
  variant: IllustrationVariant;
  primary: Landmark;
  secondary: Landmark | null;
}) {
  // Both cities are always drawn when the trip has two. An earlier cut let
  // `cropped` and `medallion` show only the first, which quietly told the
  // reader that half their trip did not matter.
  if (!secondary) return <Solo variant={variant} landmark={primary} />;

  if (variant === "medallion") {
    // Primary inside the cartographic ring, secondary standing outside it.
    return (
      <g>
        <circle cx="64" cy={GROUND_Y - 30} r="42" className="cityart__frame" />
        <g transform={`translate(${64 - primary.width / 2}, -14) scale(0.9)`}>
          <Drawing landmark={primary} />
        </g>
        <g
          transform={`translate(128, ${GROUND_Y * 0.14}) scale(0.8)`}
          className="cityart__far"
        >
          <Drawing landmark={secondary} />
        </g>
      </g>
    );
  }

  if (variant === "cropped") {
    // Enlarged until the first landmark runs past the frame. The crop is the
    // composition; a whole building centred in a box is a diagram.
    return (
      <g transform={`translate(-14, ${-STAGE_H * 0.2}) scale(1.3)`}>
        <Drawing landmark={primary} />
        <g
          transform={`translate(${primary.width + 6}, ${GROUND_Y * 0.22}) scale(0.8)`}
          className="cityart__far"
        >
          <Drawing landmark={secondary} />
        </g>
      </g>
    );
  }

  // The remaining four share a ground line and differ in spacing and scale,
  // which at this size reads as a bigger difference than it sounds.
  const gap = variant === "joined" ? 16 : variant === "horizon" ? 22 : 6;
  const farScale = variant === "skyline" ? 0.92 : 0.82;
  const secondX = primary.width + gap;
  const total = secondX + secondary.width * farScale;
  return (
    <g transform={`translate(${(STAGE_W - total) / 2}, 0)`}>
      <Drawing landmark={primary} />
      <g
        transform={`translate(${secondX}, ${GROUND_Y * 0.12}) scale(${farScale})`}
        className="cityart__far"
      >
        <Drawing landmark={secondary} />
      </g>
    </g>
  );
}

/** One city, for the single-destination trips the engine also returns. */
function Solo({
  variant,
  landmark,
}: {
  variant: IllustrationVariant;
  landmark: Landmark;
}) {
  if (variant === "medallion") {
    return (
      <g>
        <circle cx={STAGE_W / 2} cy={GROUND_Y - 30} r="44" className="cityart__frame" />
        <g transform={`translate(${STAGE_W / 2 - landmark.width / 2}, -14)`}>
          <Drawing landmark={landmark} />
        </g>
      </g>
    );
  }
  if (variant === "cropped") {
    return (
      <g transform={`translate(${STAGE_W * 0.16}, ${-STAGE_H * 0.26}) scale(1.5)`}>
        <Drawing landmark={landmark} />
      </g>
    );
  }
  const x = variant === "offset" ? STAGE_W * 0.5 : (STAGE_W - landmark.width) / 2;
  return (
    <g transform={`translate(${x}, 0)`}>
      <Drawing landmark={landmark} />
    </g>
  );
}

function Drawing({ landmark }: { landmark: Landmark }) {
  return (
    <g className="cityart__landmark">
      {landmark.anchor && (
        <path d={landmark.anchor} className="cityart__anchor" />
      )}
      {landmark.d.map((d, i) => (
        <path key={i} d={d} className="cityart__line" />
      ))}
    </g>
  );
}

/** Geometry behind the drawing: sand and sage, never competing with it. */
function Backdrop({ variant }: { variant: IllustrationVariant }) {
  const y = GROUND_Y - 10;
  const shift =
    variant === "offset" ? 18 : variant === "cropped" ? -12 : variant === "horizon" ? 8 : 0;

  return (
    <g className={`cityart__backdrop cityart__backdrop--${variant}`}>
      <path
        d={`M-18 ${y + shift} C 28 ${y - 28 + shift}, 58 ${y + 12}, 104 ${y - 8} S 166 ${y - 28}, 220 ${y - 2}`}
        className="cityart__contour"
      />
      <path
        d={`M-12 ${y + 10 + shift} C 36 ${y - 4}, 72 ${y + 24}, 116 ${y + 4} S 174 ${y - 5}, 214 ${y + 12}`}
        className="cityart__contour cityart__contour--soft"
      />
      <line x1="18" y1="18" x2="182" y2="18" className="cityart__datum" />
      <line x1="182" y1="14" x2="182" y2="22" className="cityart__datum" />
      <circle cx="182" cy="18" r="2.4" className="cityart__point" />
    </g>
  );
}

/**
 * The signature: a coral line that leaves the route, bows around the drawing
 * and rejoins it. The product's whole claim is the trip you would not have
 * searched for, and this is that sentence drawn in one stroke.
 */
function DetourLine({ variant }: { variant: IllustrationVariant }) {
  const d =
    variant === "medallion"
      ? `M2 ${GROUND_Y + 6}q40 0 58 -30q18 -30 62 -30q40 0 76 24`
      : variant === "cropped"
        ? `M-4 ${GROUND_Y + 4}q52 4 84 -26q30 -28 72 -14q28 9 52 30`
        : `M-4 ${GROUND_Y + 5}q46 2 74 -22q26 -22 62 -12q30 8 72 26`;
  return <path d={d} className="cityart__detour" />;
}

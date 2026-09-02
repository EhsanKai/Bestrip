import type { ReactElement } from "react";

/**
 * One icon family, drawn here rather than pulled from a package.
 *
 * Part 20 asks for a coherent system and warns against mixing families. The
 * surest way to keep that promise is to own the set: every glyph below is a
 * 24x24 stroked path with the same weight and terminals, so they sit together
 * on a card without one looking heavier than its neighbours.
 */

interface IconProps {
  size?: number;
  className?: string;
}

function svg(path: ReactElement, { size = 18, className = "" }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {path}
    </svg>
  );
}

export const Icon = {
  plane: (p: IconProps = {}) =>
    svg(<path d="M17.8 19.2 16 11l3.5-3.5a2.1 2.1 0 0 0-3-3L13 8 4.8 6.2a.5.5 0 0 0-.5.8l3.3 3.9-2 2H3.2a.4.4 0 0 0-.3.7l2.4 2 2 2.4a.4.4 0 0 0 .7-.3v-2.4l2-2 3.9 3.3a.5.5 0 0 0 .8-.5Z" />, p),
  train: (p: IconProps = {}) =>
    svg(
      <>
        <rect x="5" y="3" width="14" height="13" rx="3" />
        <path d="M5 10h14M9 20l-2 2M15 20l2 2M8.5 16h.01M15.5 16h.01" />
      </>,
      p,
    ),
  bus: (p: IconProps = {}) =>
    svg(
      <>
        <rect x="4" y="4" width="16" height="12" rx="2" />
        <path d="M4 10h16M7 20v-2M17 20v-2M8 13h.01M16 13h.01" />
      </>,
      p,
    ),
  hotel: (p: IconProps = {}) =>
    svg(
      <>
        <path d="M3 20V6a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v14" />
        <path d="M14 10h6a1 1 0 0 1 1 1v9M3 20h18M7 9h3M7 13h3" />
      </>,
      p,
    ),
  map: (p: IconProps = {}) =>
    svg(
      <>
        <path d="m9 4-6 2v14l6-2 6 2 6-2V4l-6 2Z" />
        <path d="M9 4v14M15 6v14" />
      </>,
      p,
    ),
  calendar: (p: IconProps = {}) =>
    svg(
      <>
        <rect x="3" y="5" width="18" height="16" rx="2" />
        <path d="M3 10h18M8 3v4M16 3v4" />
      </>,
      p,
    ),
  wallet: (p: IconProps = {}) =>
    svg(
      <>
        <path d="M3 7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v1" />
        <rect x="3" y="7" width="18" height="12" rx="2" />
        <path d="M16 13h.01" />
      </>,
      p,
    ),
  heart: (p: IconProps = {}) =>
    svg(
      <path d="M12 20s-7-4.35-7-9a4 4 0 0 1 7-2.65A4 4 0 0 1 19 11c0 4.65-7 9-7 9Z" />,
      p,
    ),
  heartFilled: ({ size = 18, className = "" }: IconProps = {}) => (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 20s-7-4.35-7-9a4 4 0 0 1 7-2.65A4 4 0 0 1 19 11c0 4.65-7 9-7 9Z" />
    </svg>
  ),
  star: (p: IconProps = {}) =>
    svg(<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.6-4.8 2.6.9-5.4L4.2 9.7l5.4-.8Z" />, p),
  lock: (p: IconProps = {}) =>
    svg(
      <>
        <rect x="4" y="10" width="16" height="10" rx="2" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3" />
      </>,
      p,
    ),
  clock: (p: IconProps = {}) =>
    svg(
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 7v5.2l3.2 2" />
      </>,
      p,
    ),
  location: (p: IconProps = {}) =>
    svg(
      <>
        <path d="M19 10c0 5.2-7 11-7 11s-7-5.8-7-11a7 7 0 1 1 14 0Z" />
        <circle cx="12" cy="10" r="2.5" />
      </>,
      p,
    ),
  route: (p: IconProps = {}) =>
    svg(
      <>
        <circle cx="6" cy="18" r="2.5" />
        <circle cx="18" cy="6" r="2.5" />
        <path d="M8.5 18H14a3.5 3.5 0 0 0 0-7h-4a3.5 3.5 0 0 1 0-7h5.5" />
      </>,
      p,
    ),
  search: (p: IconProps = {}) =>
    svg(
      <>
        <circle cx="11" cy="11" r="6.5" />
        <path d="m16 16 4.5 4.5" />
      </>,
      p,
    ),
  sparkles: (p: IconProps = {}) =>
    svg(
      <>
        <path d="m12 3 1.8 4.7L18.5 9.5 13.8 11.3 12 16l-1.8-4.7L5.5 9.5l4.7-1.8Z" />
        <path d="M18.5 15.5 19.4 18l2.5.9-2.5.9-.9 2.5-.9-2.5-2.5-.9 2.5-.9Z" />
      </>,
      p,
    ),
  arrowRight: (p: IconProps = {}) =>
    svg(<path d="M5 12h13M13 6l6 6-6 6" />, p),
  arrowDown: (p: IconProps = {}) =>
    svg(<path d="M12 5v13M6 12l6 6 6-6" />, p),
  check: (p: IconProps = {}) => svg(<path d="m5 13 4.5 4.5L19 7" />, p),
  close: (p: IconProps = {}) => svg(<path d="M6 6l12 12M18 6 6 18" />, p),
  chevronDown: (p: IconProps = {}) => svg(<path d="m6 9 6 6 6-6" />, p),
  sliders: (p: IconProps = {}) =>
    svg(
      <>
        <path d="M5 21v-7M5 10V3M12 21v-10M12 7V3M19 21v-4M19 13V3" />
        <path d="M2.5 14h5M9.5 7h5M16.5 17h5" />
      </>,
      p,
    ),
  people: (p: IconProps = {}) =>
    svg(
      <>
        <circle cx="9" cy="8" r="3.2" />
        <path d="M3 20a6 6 0 0 1 12 0M16.5 5.3a3.2 3.2 0 0 1 0 5.4M18 20a6 6 0 0 0-2.2-4.6" />
      </>,
      p,
    ),
  compare: (p: IconProps = {}) =>
    svg(
      <>
        <path d="M12 3v18M6 8 3 12l3 4M18 8l3 4-3 4" />
      </>,
      p,
    ),
  alert: (p: IconProps = {}) =>
    svg(
      <>
        <path d="M12 4.5 21 20H3Z" />
        <path d="M12 10v4M12 17h.01" />
      </>,
      p,
    ),
};

export function ModeIcon({ mode, size = 18 }: { mode: string; size?: number }) {
  if (mode === "train") return Icon.train({ size });
  if (mode === "bus") return Icon.bus({ size });
  return Icon.plane({ size });
}

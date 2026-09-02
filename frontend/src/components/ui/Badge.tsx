import type { ReactNode } from "react";
import type {
  AvailabilityStatus,
  ConfidenceLevel,
  IntensityBand,
  PriceFreshness,
} from "../../api/types";
import {
  AVAILABILITY_LABELS,
  CONFIDENCE_LABELS,
  FRESHNESS_LABELS,
  INTENSITY_LABELS,
} from "../../lib/format";
import "./Badge.css";

type Tone = "neutral" | "accent" | "sage" | "sand" | "success" | "warning" | "error";

interface BadgeProps {
  tone?: Tone;
  icon?: ReactNode;
  children: ReactNode;
  title?: string;
}

export function Badge({ tone = "neutral", icon, children, title }: BadgeProps) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {icon}
      {children}
    </span>
  );
}

/**
 * Trust badges (Part 25). Each of these answers a different question and none
 * of them is decorative:
 *
 *  - freshness: how old is this price?
 *  - availability: can it still be booked?
 *  - confidence: how well founded is the recommendation?
 *
 * Every one carries a `title` with the full sentence, and none communicates
 * through colour alone - the text says it too, which is what makes them
 * legible to a screen reader and to anyone who cannot distinguish the tones.
 */

export function PriceFreshnessBadge({ status }: { status: PriceFreshness }) {
  const tone: Tone =
    status === "FRESH"
      ? "success"
      : status === "RECENT"
        ? "sage"
        : status === "STALE"
          ? "warning"
          : "neutral";
  return (
    <Badge tone={tone} title={FRESHNESS_LABELS[status]} icon={<ClockIcon />}>
      {status === "UNKNOWN" ? "Estimated" : FRESHNESS_LABELS[status]}
    </Badge>
  );
}

export function AvailabilityBadge({ status }: { status: AvailabilityStatus }) {
  if (status === "UNKNOWN") {
    // Silence is the honest render: we were never told, and inventing
    // reassurance is worse than saying nothing.
    return null;
  }
  const tone: Tone =
    status === "AVAILABLE" ? "success" : status === "LIMITED" ? "warning" : "error";
  return (
    <Badge tone={tone} title={AVAILABILITY_LABELS[status]}>
      {AVAILABILITY_LABELS[status]}
    </Badge>
  );
}

export function ConfidenceBadge({ level }: { level: ConfidenceLevel }) {
  const tone: Tone =
    level === "HIGH" ? "success" : level === "GOOD" ? "sage" : "warning";
  return <Badge tone={tone}>{CONFIDENCE_LABELS[level]}</Badge>;
}

export function IntensityBadge({ band }: { band: IntensityBand }) {
  const tone: Tone = band === "LOW" ? "sage" : band === "MODERATE" ? "sand" : "warning";
  return <Badge tone={tone}>{INTENSITY_LABELS[band]} pace</Badge>;
}

function ClockIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
      <path
        d="M12 7v5l3 2"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

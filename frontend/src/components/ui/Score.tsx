import { matchBand, percent } from "../../lib/format";
import "./Score.css";

interface Props {
  label: string;
  /** 0-1. */
  value: number;
  /** Show the band phrase instead of the number (V5.7). */
  qualitative?: boolean;
  tone?: "accent" | "sage" | "neutral";
}

/**
 * A score with its meaning attached.
 *
 * V5.7 is explicit that users should read "Excellent match for your interests"
 * rather than "Culture = 0.84". The bar is the visual, the percentage is the
 * detail, and `qualitative` swaps the number for the phrase where the phrase
 * is what actually helps.
 */
export function Score({ label, value, qualitative = false, tone = "accent" }: Props) {
  const pct = Math.max(0, Math.min(1, value));
  return (
    <div className="score">
      <div className="score__head">
        <span className="score__label">{label}</span>
        <span className="score__value numeric">
          {qualitative ? matchBand(value) : percent(value)}
        </span>
      </div>
      <div
        className={`score__track score__track--${tone}`}
        role="meter"
        aria-valuenow={Math.round(pct * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label}: ${matchBand(value)}`}
      >
        <div className="score__fill" style={{ width: `${pct * 100}%` }} />
      </div>
    </div>
  );
}

/** The compact inline form used on dense cards. */
export function ScorePill({ label, value }: { label: string; value: number }) {
  return (
    <div className="score-pill">
      <span className="score-pill__value numeric">{percent(value)}</span>
      <span className="score-pill__label">{label}</span>
    </div>
  );
}

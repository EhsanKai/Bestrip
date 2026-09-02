import type { ButtonHTMLAttributes, ReactNode } from "react";
import "./Button.css";

type Variant = "primary" | "secondary" | "ghost" | "quiet";
type Size = "sm" | "md" | "lg";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
  iconAfter?: ReactNode;
  loading?: boolean;
  full?: boolean;
}

/**
 * Coral is the primary action and nothing else. A screen with two coral
 * buttons has no primary action, so `secondary` is the outlined navy one and
 * `ghost` is for tertiary moves that should not compete at all.
 */
export function Button({
  variant = "primary",
  size = "md",
  icon,
  iconAfter,
  loading = false,
  full = false,
  children,
  className = "",
  disabled,
  ...rest
}: Props) {
  return (
    <button
      className={`btn btn--${variant} btn--${size} ${full ? "btn--full" : ""} ${className}`}
      disabled={disabled || loading}
      // The label still reads normally to a screen reader while busy; only
      // the visual affordance changes.
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span className="btn__spinner" aria-hidden="true" /> : icon}
      <span className="btn__label">{children}</span>
      {iconAfter}
    </button>
  );
}

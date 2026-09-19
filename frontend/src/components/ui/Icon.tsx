import {
  AirplaneTilt, Train, Bus, Buildings, MapTrifold, CalendarBlank, Wallet,
  Heart, Star, LockSimple, Clock, MapPin, Path, MagnifyingGlass, Sparkle,
  ArrowRight, ArrowDown, Check, X, CaretDown, SlidersHorizontal, Users,
  ArrowsLeftRight, Warning, Sun, Moon,
} from "@phosphor-icons/react";
import type { Icon as PhosphorIcon } from "@phosphor-icons/react";

interface IconProps { size?: number; className?: string }
const glyph = (Component: PhosphorIcon, fill = false) =>
  ({ size = 18, className = "" }: IconProps = {}) =>
    <Component size={size} className={className} weight={fill ? "fill" : "regular"} aria-hidden="true" />;

export const Icon = {
  plane: glyph(AirplaneTilt), train: glyph(Train), bus: glyph(Bus),
  hotel: glyph(Buildings), map: glyph(MapTrifold), calendar: glyph(CalendarBlank),
  wallet: glyph(Wallet), heart: glyph(Heart), heartFilled: glyph(Heart, true),
  star: glyph(Star), lock: glyph(LockSimple), clock: glyph(Clock), location: glyph(MapPin),
  route: glyph(Path), search: glyph(MagnifyingGlass), sparkles: glyph(Sparkle),
  arrowRight: glyph(ArrowRight), arrowDown: glyph(ArrowDown), check: glyph(Check),
  close: glyph(X), chevronDown: glyph(CaretDown), sliders: glyph(SlidersHorizontal),
  people: glyph(Users), compare: glyph(ArrowsLeftRight), alert: glyph(Warning),
  sun: glyph(Sun), moon: glyph(Moon),
};
export function ModeIcon({ mode, size = 18 }: { mode: string; size?: number }) {
  if (mode === "train") return Icon.train({ size });
  if (mode === "bus") return Icon.bus({ size });
  return Icon.plane({ size });
}

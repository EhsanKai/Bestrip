import { Icon } from "../ui/Icon";
import "./MobileNav.css";

interface Props {
  screen: string;
  savedCount: number;
  hasResults: boolean;
  onNavigate: (screen: string) => void;
}

/**
 * Bottom navigation (Part 18).
 *
 * Mobile is not a narrower desktop: the primary destinations move to the thumb
 * zone, and "Results" only appears once there are results to go back to —
 * a permanent tab that leads nowhere is worse than one less tab.
 */
export function MobileNav({ screen, savedCount, hasResults, onNavigate }: Props) {
  const items = [
    { id: "landing", label: "Home", icon: Icon.map({ size: 20 }) },
    { id: "discover", label: "Discover", icon: Icon.search({ size: 20 }) },
    ...(hasResults
      ? [{ id: "results", label: "Trips", icon: Icon.route({ size: 20 }) }]
      : []),
    { id: "saved", label: "Saved", icon: Icon.heart({ size: 20 }), count: savedCount },
  ];

  return (
    <nav className="mobilenav" aria-label="Primary">
      {items.map((item) => (
        <button
          key={item.id}
          className={`mobilenav__item ${screen === item.id ? "is-on" : ""}`}
          onClick={() => onNavigate(item.id)}
          aria-current={screen === item.id ? "page" : undefined}
        >
          <span className="mobilenav__icon">
            {item.icon}
            {"count" in item && item.count ? (
              <span className="mobilenav__dot" aria-hidden="true" />
            ) : null}
          </span>
          <span className="mobilenav__label">{item.label}</span>
        </button>
      ))}
    </nav>
  );
}

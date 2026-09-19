import { BrandLogo } from "../ui/BrandLogo";
import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import "./Header.css";

interface Props {
  onHome: () => void;
  onDiscover: () => void;
  onSaved: () => void;
  onResults: () => void;
  onJourney: () => void;
  onAccount: () => void;
  savedCount: number;
  showSearchNav: boolean;
  journeySummary?: string | null;
  journeyExists?: boolean;
  accountLabel?: string;
}

export function Header({
  onHome,
  onDiscover,
  onSaved,
  onResults,
  onJourney,
  onAccount,
  savedCount,
  showSearchNav,
  journeySummary,
  journeyExists = false,
  accountLabel = "Account",
}: Props) {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      const stored = localStorage.getItem("detoura-theme");
      if (stored === "dark" || stored === "light") return stored;
    } catch { /* Storage may be unavailable. */ }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("detoura-theme", theme);
    } catch {
      /* private mode: the choice just does not persist */
    }
  }, [theme]);

  return (
    <header className="header">
      <div className="container header__inner">
        <button className="header__brand" onClick={onHome} aria-label="Detoura home">
          <BrandLogo />
        </button>

        <nav className="header__nav" aria-label="Main">
          <button className="header__link" onClick={onDiscover}>
            Discover
          </button>
          {showSearchNav && (
            <button className="header__link" onClick={onResults}>
              Results
            </button>
          )}
          <button className="header__link" onClick={onSaved}>
            My Trips
            {savedCount > 0 && !journeyExists && <span className="header__count">{savedCount}</span>}
          </button>
          <button
            className={`header__link header__journey ${journeyExists ? "header__journey--active" : ""}`}
            onClick={onJourney}
          >
            {Icon.route({ size: 16 })}
            <span>{journeySummary ?? "Your Journey"}</span>
          </button>
          <button className="header__link" onClick={onAccount}>
            {accountLabel}
          </button>
        </nav>

        <div className="header__actions">
          <button
            className={`header__journey-action ${journeyExists ? "header__journey-action--active" : ""}`}
            type="button"
            onClick={onJourney}
            aria-label={journeySummary ? `Open ${journeySummary}` : "Open Your Journey"}
          >
            {Icon.route({ size: 17 })}
            <span>{journeyExists ? "Journey" : "Your Journey"}</span>
          </button>
          <button
            className="header__icon-btn"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? Icon.sun() : Icon.moon()}
          </button>
          <Button size="sm" onClick={onDiscover} className="header__discover">
            Discover
          </Button>
        </div>
      </div>
    </header>
  );
}

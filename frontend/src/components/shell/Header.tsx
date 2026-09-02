import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import "./Header.css";

interface Props {
  onHome: () => void;
  onDiscover: () => void;
  onSaved: () => void;
  onResults: () => void;
  savedCount: number;
  showSearchNav: boolean;
}

export function Header({
  onHome,
  onDiscover,
  onSaved,
  onResults,
  savedCount,
  showSearchNav,
}: Props) {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const stored = localStorage.getItem("detoura-theme");
    return stored === "dark" || stored === "light" ? stored : "light";
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
          <span className="header__mark" aria-hidden="true">
            {Icon.route({ size: 20 })}
          </span>
          <span className="header__word">Detoura</span>
        </button>

        <nav className="header__nav" aria-label="Main">
          {showSearchNav && (
            <button className="header__link" onClick={onResults}>
              Results
            </button>
          )}
          <button className="header__link" onClick={onSaved}>
            Saved
            {savedCount > 0 && <span className="header__count">{savedCount}</span>}
          </button>
        </nav>

        <div className="header__actions">
          <button
            className="header__icon-btn"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
          <Button size="sm" onClick={onDiscover}>
            Discover
          </Button>
        </div>
      </div>
    </header>
  );
}

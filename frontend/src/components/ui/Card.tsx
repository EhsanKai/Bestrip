import type { HTMLAttributes, ReactNode } from "react";
import "./Card.css";

interface Props extends HTMLAttributes<HTMLElement> {
  as?: "div" | "article" | "section" | "li";
  interactive?: boolean;
  selected?: boolean;
  padded?: boolean;
  children: ReactNode;
}

/**
 * The surface everything sits on. Restrained by design: a hairline border and
 * a shadow you have to look for. Elevation is for the thing under the cursor,
 * not for every card on the page.
 */
export function Card({
  as: Tag = "div",
  interactive = false,
  selected = false,
  padded = true,
  className = "",
  children,
  ...rest
}: Props) {
  // One cast, here, rather than a generic that would make every call site
  // declare its element type for no benefit.
  const Element = Tag as "div";
  return (
    <Element
      className={[
        "card",
        interactive && "card--interactive",
        selected && "card--selected",
        padded && "card--padded",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      {...rest}
    >
      {children}
    </Element>
  );
}

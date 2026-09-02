import { ModeIcon } from "../ui/Icon";
import "./RouteLine.css";

interface Props {
  nodes: string[];
  modes?: string[];
  /** Cities get emphasis; airports are waypoints. */
  cities?: string[];
  compact?: boolean;
}

/**
 * The route as a line, not a string.
 *
 * A route is the most repeated element in this product, and rendering it as
 * "CGN -> Munich -> Vienna -> CGN" wastes the one chance to make the shape of
 * a trip legible at a glance. Here the origin and return airports are quiet
 * waypoints and the cities are the destinations - which is the actual
 * hierarchy of a trip, and the reason a two-city detour reads differently from
 * a there-and-back.
 */
export function RouteLine({ nodes, modes = [], cities = [], compact = false }: Props) {
  const cityset = new Set(cities);
  return (
    <div className={`route ${compact ? "route--compact" : ""}`}>
      {nodes.map((node, index) => {
        const isCity = cityset.size ? cityset.has(node) : index > 0 && index < nodes.length - 1;
        return (
          <div className="route__segment" key={`${node}-${index}`}>
            <span className={`route__node ${isCity ? "route__node--city" : ""}`}>
              {isCity && <span className="route__dot" aria-hidden="true" />}
              {node}
            </span>
            {index < nodes.length - 1 && (
              <span className="route__link" aria-hidden="true">
                <span className="route__rule" />
                <span className="route__mode">
                  <ModeIcon mode={modes[index] ?? "flight"} size={compact ? 12 : 14} />
                </span>
                <span className="route__rule" />
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

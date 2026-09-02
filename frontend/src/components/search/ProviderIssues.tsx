import type { ProviderIssue } from "../../api/types";
import { Icon } from "../ui/Icon";
import "./States.css";

/**
 * Shown *above real results*, never instead of them.
 *
 * This is the visible half of V5.1.1. When a provider degrades, the trips
 * below are genuine — they were simply found with less information than we
 * wanted — so the honest render is a banner that qualifies them, not an error
 * page that discards them.
 */
export function ProviderIssues({ issues }: { issues: ProviderIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <div className="banner banner--warn" role="status">
      <span className="banner__icon">{Icon.alert({ size: 18 })}</span>
      <div>
        <strong>These results may be incomplete.</strong>
        <ul className="banner__list">
          {issues.map((issue, index) => (
            <li key={index}>
              {issue.message}
              {issue.retryable && " Trying again may help."}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

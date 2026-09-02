import type { BaselineComparisonDTO } from "../../api/types";
import { hours, joinCities, money, signedMoney } from "../../lib/format";
import { Icon } from "../ui/Icon";
import "./BaselineComparison.css";

type Baseline = BaselineComparisonDTO;

interface Props {
  baseline: Baseline;
  ourCities: string[];
  ourPrice: number;
  ourUsableHours: number;
  currency?: string;
}

/**
 * The signature Detoura component (Part 9).
 *
 * The entire brand argument lives in this one layout: *your original idea is
 * good; here is what else the same budget buys*. Two things follow from that
 * and both are deliberate.
 *
 * First, the traveler's own idea is shown **first and in full**, not as a
 * struck-through "before" price. It is not a mistake being corrected.
 *
 * Second, the deltas are shown in **both directions**, including the one that
 * makes us look worse. A component that reported "+1 city, +20 hours" and hid
 * "+€71" would be an advertisement. Showing all three is what makes it advice.
 */
export function BaselineComparison({
  baseline,
  ourCities,
  ourPrice,
  ourUsableHours,
  currency = "EUR",
}: Props) {
  const priceDelta = baseline.price_delta;
  const cityDelta = baseline.extra_cities;
  const hoursDelta = baseline.extra_usable_hours;

  return (
    <section className="baseline" aria-labelledby="baseline-heading">
      <h2 id="baseline-heading" className="eyebrow baseline__heading">
        Your idea, and another possibility
      </h2>

      <div className="baseline__pair">
        <div className="baseline__side">
          <div className="baseline__label subtle">Your original idea</div>
          <div className="baseline__place">{baseline.destination}</div>
          <div className="baseline__price numeric">
            {money(baseline.total_price, currency)}
          </div>
          <ul className="baseline__facts subtle">
            <li>1 city</li>
            <li className="numeric">{hours(baseline.usable_hours)} usable</li>
            <li className="numeric">{baseline.nights} nights</li>
          </ul>
        </div>

        <div className="baseline__arrow" aria-hidden="true">
          {Icon.arrowDown({ size: 20 })}
        </div>

        <div className="baseline__side baseline__side--ours">
          <div className="baseline__label">Detoura found</div>
          <div className="baseline__place">{joinCities(ourCities)}</div>
          <div className="baseline__price numeric">{money(ourPrice, currency)}</div>
          <ul className="baseline__facts">
            <li>
              {ourCities.length} {ourCities.length === 1 ? "city" : "cities"}
            </li>
            <li className="numeric">{hours(ourUsableHours)} usable</li>
          </ul>
        </div>
      </div>

      <ul className="baseline__deltas">
        <Delta
          // Rounded: the delta chip sits beside a sentence that says
          // "71 EUR more", and two spellings of one number reads as sloppy.
          value={signedMoney(Math.round(priceDelta), currency)}
          label={priceDelta > 0 ? "more to spend" : "saved"}
          tone={priceDelta > 0 ? "cost" : "gain"}
        />
        {cityDelta !== 0 && (
          <Delta
            value={`${cityDelta > 0 ? "+" : ""}${cityDelta}`}
            label={Math.abs(cityDelta) === 1 ? "city" : "cities"}
            tone="gain"
          />
        )}
        <Delta
          value={`${hoursDelta > 0 ? "+" : ""}${hoursDelta.toFixed(0)}h`}
          label="usable time"
          tone={hoursDelta >= 0 ? "gain" : "cost"}
        />
      </ul>

      <p className="baseline__note subtle">
        {baseline.destination} isn't a bad choice. This is what else your budget
        can buy.
      </p>
    </section>
  );
}

function Delta({
  value,
  label,
  tone,
}: {
  value: string;
  label: string;
  tone: "gain" | "cost";
}) {
  return (
    <li className={`baseline__delta baseline__delta--${tone}`}>
      <span className="baseline__delta-value numeric">{value}</span>
      <span className="baseline__delta-label">{label}</span>
    </li>
  );
}

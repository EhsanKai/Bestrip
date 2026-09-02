import type { LegSummary, StaySummary, TripRecommendation } from "../../api/types";
import { clockTime, minutesAsHours, money, weekdayLong } from "../../lib/format";
import { ModeIcon } from "../ui/Icon";
import "./Timeline.css";

interface Entry {
  kind: "travel" | "stay";
  at: string;
  leg?: LegSummary;
  stay?: StaySummary;
}

/**
 * The day-by-day itinerary (Part 13).
 *
 * The one visual rule that matters here: **travel time and destination time
 * must not look the same.** A traveller reading this needs to see instantly
 * which parts of the trip are the trip and which parts are getting there, so
 * travel entries are quiet and rule-like while stays are solid blocks with the
 * usable hours called out. That distinction is most of what separates this
 * from a booking confirmation.
 */
export function Timeline({ trip }: { trip: TripRecommendation }) {
  const entries: Entry[] = [
    ...trip.legs.map<Entry>((leg) => ({
      kind: "travel",
      at: leg.departure,
      leg,
    })),
    ...trip.stays.map<Entry>((stay) => ({
      kind: "stay",
      at: stay.arrival,
      stay,
    })),
  ].sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());

  // A day heading is shown when an entry starts a calendar day, which is a
  // property of the entry and its predecessor - derived here rather than
  // carried in a variable mutated during render.
  const day = (entry: Entry) => new Date(entry.at).toDateString();

  return (
    <ol className="timeline">
      {entries.map((entry, index) => {
        const newDay = index === 0 || day(entry) !== day(entries[index - 1]);

        return (
          <li key={index} className={`timeline__item timeline__item--${entry.kind}`}>
            {newDay && (
              <div className="timeline__day eyebrow">{weekdayLong(entry.at)}</div>
            )}

            {entry.kind === "travel" && entry.leg && (
              <div className="timeline__travel">
                <span className="timeline__icon" aria-hidden="true">
                  <ModeIcon mode={entry.leg.mode} size={16} />
                </span>
                <div className="timeline__travel-body">
                  <div className="timeline__travel-route">
                    <strong>{entry.leg.from}</strong>
                    <span aria-hidden="true"> → </span>
                    <strong>{entry.leg.to}</strong>
                  </div>
                  <div className="timeline__travel-meta subtle">
                    <span className="numeric">{clockTime(entry.leg.departure)}</span>
                    <span aria-hidden="true">–</span>
                    <span className="numeric">{clockTime(entry.leg.arrival)}</span>
                    <span>·</span>
                    <span className="numeric">{minutesAsHours(entry.leg.minutes)}</span>
                    <span>·</span>
                    <span>{entry.leg.mode}</span>
                    {entry.leg.seats_available !== null &&
                      entry.leg.seats_available <= 4 && (
                        <>
                          <span>·</span>
                          <span className="timeline__scarce">
                            {entry.leg.seats_available} seats left
                          </span>
                        </>
                      )}
                  </div>
                </div>
                <span className="timeline__price subtle numeric">
                  {money(entry.leg.price_per_person)}/person
                </span>
              </div>
            )}

            {entry.kind === "stay" && entry.stay && (
              <div className="timeline__stay">
                <div className="timeline__stay-head">
                  <h3 className="timeline__city">{entry.stay.city}</h3>
                  <span className="timeline__nights subtle">
                    {entry.stay.nights} {entry.stay.nights === 1 ? "night" : "nights"}
                  </span>
                </div>

                <div className="timeline__usable">
                  <span className="timeline__usable-bar" aria-hidden="true" />
                  <span>
                    <strong className="numeric">
                      {(entry.stay.usable_minutes / 60).toFixed(1)}h
                    </strong>{" "}
                    of usable time here
                  </span>
                </div>

                {entry.stay.name && (
                  <div className="timeline__hotel">
                    <div>
                      <div className="timeline__hotel-name">{entry.stay.name}</div>
                      <div className="subtle timeline__hotel-meta">
                        {entry.stay.tier && <span>{entry.stay.tier}</span>}
                        {entry.stay.rating !== null && (
                          <span className="numeric">
                            {(entry.stay.rating * 5).toFixed(1)}★
                          </span>
                        )}
                        {entry.stay.free_cancellation && <span>free cancellation</span>}
                        {entry.stay.rooms_available !== null &&
                          entry.stay.rooms_available <= 3 && (
                            <span className="timeline__scarce">
                              {entry.stay.rooms_available} rooms left
                            </span>
                          )}
                      </div>
                    </div>
                    <span className="numeric timeline__hotel-price">
                      {money(entry.stay.cost)}
                    </span>
                  </div>
                )}

                {entry.stay.value_note && (
                  <p className="timeline__value-note subtle">{entry.stay.value_note}</p>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

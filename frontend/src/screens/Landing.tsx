import { Button } from "../components/ui/Button";
import { Icon } from "../components/ui/Icon";
import { RouteMap } from "../components/trip/RouteMap";
import "./Landing.css";

interface Props {
  onDiscover: () => void;
}

/**
 * The landing page has one job: land the argument in six words, then show
 * rather than tell.
 *
 * The hero visual is the product's actual output - three real routes from the
 * synthetic network with their real prices - not a stock photograph of a
 * beach. That is the whole positioning: Detoura is about routes you would not
 * have thought of, so the hero shows routes you would not have thought of.
 */
export function Landing({ onDiscover }: Props) {
  return (
    <div className="landing">
      <section className="landing__hero">
        <div className="container landing__hero-inner">
          <div className="landing__copy">
            <p className="landing__eyebrow">Travel discovery &amp; optimization</p>
            <h1 className="display landing__title">
              Don't take the
              <br />
              <span className="landing__title-accent">obvious trip.</span>
            </h1>
            <p className="landing__lead">
              Tell Detoura your budget, dates and travel style. We'll explore
              destinations, routes and stays to find trips worth taking.
            </p>
            <div className="landing__actions">
              <Button size="lg" onClick={onDiscover} iconAfter={Icon.arrowRight({ size: 18 })}>
                Discover my trip
              </Button>
              <Button
                size="lg"
                variant="quiet"
                onClick={() => {
                  document
                    .getElementById("how-it-works")
                    ?.scrollIntoView({ behavior: "smooth" });
                }}
              >
                See how it works
              </Button>
            </div>
          </div>

          <div className="landing__visual">
            <RouteMap
              routes={[
                { nodes: ["Cologne", "Brussels"], price: 242, label: "1 city" },
                {
                  nodes: ["Cologne", "Munich", "Vienna"],
                  price: 410,
                  label: "2 cities",
                  highlight: true,
                },
                {
                  nodes: ["Düsseldorf", "Budapest", "Vienna"],
                  price: 403,
                  label: "2 cities",
                },
              ]}
            />
          </div>
        </div>
      </section>

      <section className="landing__pitch" id="how-it-works">
        <div className="container">
          <h2 className="h2 landing__pitch-title">
            You don't have to know where you want to go.
          </h2>
          <p className="lead landing__pitch-lead">
            A conventional search asks where you're going. Detoura asks how you
            want to travel — then works out which trips are actually worth
            taking.
          </p>

          <ol className="landing__steps">
            <Step
              n="01"
              icon={Icon.wallet({ size: 20 })}
              title="Tell us the constraints"
              body="Where you start, when you're free, what you can spend, who's coming."
            />
            <Step
              n="02"
              icon={Icon.route({ size: 20 })}
              title="We explore the alternatives"
              body="Whole trips, not single flights: airports, routes, stays, and the days you actually get on the ground."
            />
            <Step
              n="03"
              icon={Icon.sparkles({ size: 20 })}
              title="You see what else your budget buys"
              body="Every recommendation is compared against the trip you had in mind, in both directions."
            />
          </ol>
        </div>
      </section>

      <section className="landing__proof">
        <div className="container landing__proof-inner">
          <blockquote className="landing__quote">
            <p>
              You thought <strong>Cologne → Madrid</strong>. For €71 more, the
              same week buys <strong>Munich + Vienna</strong> — a second city
              and 21 more hours actually spent somewhere.
            </p>
          </blockquote>
          <p className="landing__quote-note subtle">
            Madrid isn't a bad choice. That's the point — we show you the
            alternative, not a correction.
          </p>
          <Button size="lg" onClick={onDiscover} iconAfter={Icon.arrowRight({ size: 18 })}>
            Find your detour
          </Button>
        </div>
      </section>
    </div>
  );
}

function Step({
  n,
  icon,
  title,
  body,
}: {
  n: string;
  icon: React.ReactNode;
  title: string;
  body: string;
}) {
  return (
    <li className="landing__step">
      <div className="landing__step-head">
        <span className="landing__step-icon">{icon}</span>
        <span className="landing__step-n eyebrow">{n}</span>
      </div>
      <h3 className="h3">{title}</h3>
      <p className="muted">{body}</p>
    </li>
  );
}

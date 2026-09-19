import { useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { Button } from "../components/ui/Button";
import { Icon } from "../components/ui/Icon";
import "./Landing.css";

interface Props { onDiscover: () => void }
const moods = [
  { name: "A slower pace", title: "Room to breathe.", body: "Lakeside mornings. Long lunches. A little more time to simply be somewhere.", image: "/images/lake-como.webp", alt: "A lakeside villa and wooden boat beneath the Italian mountains" },
  { name: "A little culture", title: "Follow your curiosity.", body: "Wander old streets, linger over coffee, and make room for an unexpected discovery.", image: "/images/old-town.webp", alt: "A quiet stone street with a small cafe and green shutters" },
];

type HeroBackground = {
  id: string;
  poster: string;
  mobilePoster?: string;
  videoSrc?: string;
  videoType?: string;
  location: string;
  alt: string;
};

const heroBackgrounds: HeroBackground[] = [
  {
    id: "venice-grand-canal",
    poster: "/media/hero/venice-grand-canal-poster.webp",
    mobilePoster: "/media/hero/venice-grand-canal-mobile.webp",
    videoSrc: "/media/hero/venice-grand-canal.webm",
    videoType: "video/webm",
    location: "Venice, Italy",
    alt: "Santa Maria della Salute and the Grand Canal in Venice",
  },
  {
    id: "lake-como",
    poster: "/media/hero/lake-como-poster.webp",
    mobilePoster: "/media/hero/lake-como-mobile.webp",
    videoSrc: "/media/hero/lake-como.webm",
    videoType: "video/webm",
    location: "Lake Como, Italy",
    alt: "Lake Como shoreline with mountain views in northern Italy",
  },
  {
    id: "lisbon-tram",
    poster: "/media/hero/lisbon-tram-poster.webp",
    mobilePoster: "/media/hero/lisbon-tram-mobile.webp",
    videoSrc: "/media/hero/lisbon-tram.webm",
    videoType: "video/webm",
    location: "Lisbon, Portugal",
    alt: "A yellow tram moving through a Lisbon street",
  },
  {
    id: "swiss-mountains",
    poster: "/media/hero/swiss-mountains-poster.webp",
    mobilePoster: "/media/hero/swiss-mountains-mobile.webp",
    videoSrc: "/media/hero/swiss-mountains.webm",
    videoType: "video/webm",
    location: "Swiss Alps, Switzerland",
    alt: "Clouds moving across the Swiss Alps near the Matterhorn",
  },
  {
    id: "prague-vltava",
    poster: "/media/hero/prague-vltava-poster.webp",
    mobilePoster: "/media/hero/prague-vltava-mobile.webp",
    videoSrc: "/media/hero/prague-vltava.webm",
    videoType: "video/webm",
    location: "Prague, Czechia",
    alt: "The Vltava River and historic Prague skyline",
  },
  {
    id: "dubrovnik-adriatic",
    poster: "/media/hero/dubrovnik-adriatic-poster.webp",
    mobilePoster: "/media/hero/dubrovnik-adriatic-mobile.webp",
    videoSrc: "/media/hero/dubrovnik-adriatic.webm",
    videoType: "video/webm",
    location: "Dubrovnik, Croatia",
    alt: "Dubrovnik city and Adriatic harbor from above",
  },
  {
    id: "skogafoss-waterfall",
    poster: "/media/hero/skogafoss-waterfall-poster.webp",
    mobilePoster: "/media/hero/skogafoss-waterfall-mobile.webp",
    videoSrc: "/media/hero/skogafoss-waterfall.webm",
    videoType: "video/webm",
    location: "Skógafoss, Iceland",
    alt: "Skógafoss waterfall in Iceland",
  },
  {
    id: "budapest-danube",
    poster: "/media/hero/budapest-danube-poster.webp",
    mobilePoster: "/media/hero/budapest-danube-mobile.webp",
    videoSrc: "/media/hero/budapest-danube.webm",
    videoType: "video/webm",
    location: "Budapest, Hungary",
    alt: "The Danube River and Budapest skyline",
  },
];

function chooseHeroBackground() {
  return heroBackgrounds[Math.floor(Math.random() * heroBackgrounds.length)] ?? heroBackgrounds[0];
}

export function Landing({ onDiscover }: Props) {
  const reduce = useReducedMotion();
  const [mood, setMood] = useState(0);
  const [heroBackground] = useState(chooseHeroBackground);
  const showVideo = Boolean(heroBackground.videoSrc && !reduce);
  const reveal = { initial: reduce ? false as const : { opacity: 0, y: 24 }, whileInView: { opacity: 1, y: 0 }, viewport: { once: true, amount: 0.15 }, transition: { duration: 0.7 } };
  return (
    <div className="landing">
      <section className="landing__hero container landing__hero--cinematic">
        <motion.div className="landing__copy" {...reveal}>
          <p className="landing__eyebrow">More cities. A brighter you.</p>
          <h1 className="landing__title">Extraordinary trips<br />within reach.</h1>
          <p className="landing__lead">Your time is precious. Find extraordinary routes, thoughtful stays, and a journey that feels entirely yours.</p>
          <div className="landing__actions">
            <Button size="lg" onClick={onDiscover} iconAfter={Icon.arrowRight({ size: 20 })}>Discover</Button>
            <a className="landing__text-link" href="#how-it-works">How it works {Icon.arrowDown({size:16})}</a>
          </div>
        </motion.div>
        <motion.figure
          className={`landing__hero-visual landing__hero-visual--${heroBackground.id}`}
          initial={reduce ? false : { scale: 1.035 }}
          animate={{ scale: 1 }}
          transition={{ duration: 1.2 }}
          aria-hidden="true"
        >
          {showVideo ? (
            <video
              className="landing__hero-video"
              poster={heroBackground.poster}
              muted
              autoPlay
              loop
              playsInline
              preload="metadata"
            >
              <source src={heroBackground.videoSrc} type={heroBackground.videoType ?? "video/mp4"} />
            </video>
          ) : (
            <img
              src={heroBackground.poster}
              srcSet={heroBackground.mobilePoster ? `${heroBackground.mobilePoster} 800w, ${heroBackground.poster} 1536w` : undefined}
              sizes="100vw"
              alt=""
              width="1536"
              height="1024"
              fetchPriority="high"
            />
          )}
        </motion.figure>
        <div className="landing__hero-location" aria-label={`Hero background location: ${heroBackground.location}`}>
          {Icon.location({ size: 14 })}
          <span>{heroBackground.location}</span>
        </div>
      </section>

      <section className="landing__assurance container" aria-label="The Detoura approach">
        <div>{Icon.route({size:23})}<span>Whole journeys.<small>Flights, stays, and the way between.</small></span></div>
        <div>{Icon.wallet({size:23})}<span>Your budget, considered.<small>More possibilities for what you spend.</small></span></div>
        <div>{Icon.clock({size:23})}<span>Time well spent.<small>More of the trip. Less of the planning.</small></span></div>
      </section>

      <motion.section className="landing__inspiration container" {...reveal}>
        <div className="landing__section-title"><h2>A change of scenery.<br />A different kind of feeling.</h2><p>You don't have to know where. Start with how you want to feel.</p></div>
        <div className="landing__mood-tabs" role="tablist" aria-label="Travel inspiration">
          {moods.map((item, i) => <button key={item.name} id={`mood-tab-${i}`} role="tab" aria-selected={mood === i} aria-controls="mood-panel" tabIndex={mood === i ? 0 : -1} onClick={() => setMood(i)} onKeyDown={e => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) { e.preventDefault(); const next = e.key === "Home" ? 0 : e.key === "End" ? 1 : 1 - mood; setMood(next); document.getElementById(`mood-tab-${next}`)?.focus(); } }}>{item.name}</button>)}
        </div>
        <div className="landing__mood" id="mood-panel" role="tabpanel" aria-labelledby={`mood-tab-${mood}`}>
          <motion.img key={mood} src={moods[mood].image} alt={moods[mood].alt} width="1536" height="1024" loading="lazy" initial={reduce ? false : { opacity: 0.35 }} animate={{ opacity: 1 }} transition={{ duration: 0.5 }} />
          <div className="landing__mood-copy"><span className="landing__mood-icon">{mood === 0 ? Icon.map({size:30}) : Icon.hotel({size:30})}</span><h3>{moods[mood].title}</h3><p>{moods[mood].body}</p><span className="landing__inspiration-note">A little inspiration. Your results are tailored to you.</span></div>
        </div>
      </motion.section>

      <motion.section className="landing__process container" id="how-it-works" {...reveal}>
        <div className="landing__process-photo"><img src="/images/old-town.webp" alt="A sunlit cafe waiting along a quiet European street" width="1024" height="1536" loading="lazy" /></div>
        <div className="landing__process-copy"><h2>Less searching.<br />More possibility.</h2><ol>
          <li><span aria-hidden="true">{Icon.sliders({size:22})}</span><div><h3>Make it yours</h3><p>Choose your dates, budget, and the things you love.</p></div></li>
          <li><span aria-hidden="true">{Icon.route({size:22})}</span><div><h3>Take the unexpected route</h3><p>We explore destinations, connections, and stays as one complete trip.</p></div></li>
          <li><span aria-hidden="true">{Icon.heart({size:22})}</span><div><h3>Find your favourite</h3><p>Compare the possibilities. Save the ones that stay with you.</p></div></li>
        </ol></div>
      </motion.section>
      <motion.section className="landing__closing container" {...reveal}><h2>Somewhere wonderful<br />starts with you.</h2><Button size="lg" onClick={onDiscover} iconAfter={Icon.arrowRight({size:20})}>Discover</Button></motion.section>
      <footer className="landing__footer container"><span>Detoura</span><p>Don't take the obvious trip.</p><a href="#how-it-works">How it works</a></footer>
    </div>
  );
}

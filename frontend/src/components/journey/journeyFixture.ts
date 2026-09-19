import type { JourneyDrawerModel } from "./JourneyDrawer";

export const journeyDrawerFixture: JourneyDrawerModel = {
  id: "journey-prague-vienna-budapest",
  status: "selected",
  stops: [
    { city: "Prague", nights: 3, dates: "12-15 Oct" },
    { city: "Vienna", nights: 2, dates: "15-17 Oct" },
    { city: "Budapest", nights: 3, dates: "17-20 Oct" },
  ],
  dates: "12-20 Oct 2026",
  travelers: "2 travelers",
  duration: "8 nights",
  characteristics: [
    "Culture-led cities",
    "Balanced pace",
    "Train-friendly routing",
  ],
  payable: [
    { label: "Flights", amount: "€420" },
    { label: "Detoura service fee", amount: "€27" },
  ],
  payableNow: "€447",
  estimates: [
    { label: "Accommodation estimate", amount: "€310" },
    { label: "Transfers estimate", amount: "€45" },
    { label: "Extras estimate", amount: "€30" },
  ],
  estimatedTripTotal: "€832",
};

# Detoura Premium Frontend — V8 handoff

This frontend is externally designed and is the product/UI authority for V8.

## Claude integration rule
Do not redesign, restyle, replace, or simplify this frontend. Preserve its visual language and interaction architecture. Backend work may add typed API adapters and minimal state wiring required to activate the V8 screens. If the backend contract differs, prefer an adapter layer over redesigning UI.

## New V8 experience
- Premium editorial journey cards via `JourneyPoster` rather than landmark cartoons.
- Booking flow: Traveller → Review → Confirm → Booking progress.
- Explicit one-journey / one-confirmation product positioning.
- Test-mode messaging and partial-progress-ready UI.
- Responsive booking flow.

## Integration seam
`BookingExperience.tsx` intentionally uses local UI state today. Replace that state with V8 backend booking/revalidation state after backend contracts exist. Do not put provider secrets in the browser.

## Validation
`npx tsc -b --pretty false` passes in the supplied source tree.

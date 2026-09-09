# Detoura Results — Modern Editorial Redesign

This revision intentionally moves away from literal, playful city illustrations toward a restrained editorial/cartographic language.

Key changes:
- desktop search/sort rail for stronger information architecture
- Instrument Serif used only for destination/display typography; Manrope remains the UI face
- recommendation cards rebuilt as compact editorial travel proposals
- landmark SVGs retained but demoted to architectural etchings/watermarks
- removed blob-like illustration backdrops; replaced with cartographic contours and registration geometry
- visual identity remains deterministic by city pair
- price, destination and recommendation reason now dominate the hierarchy
- metrics and cost breakdown are quieter and more comparable
- navy primary trip CTA replaces shopping-like coral CTA inside result cards
- responsive mobile composition preserved

Validation in sandbox:
- TypeScript project check (`tsc -b`) passed.
- Full Vite build could not be completed in the Linux sandbox because the uploaded archive contained macOS node_modules/native Rolldown bindings and network package reinstall timed out. Run `npm ci && npm run build` on the target development machine.

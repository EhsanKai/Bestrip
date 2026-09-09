/* Which composition a trip is drawn in.
 *
 * Separate from the component so that file exports components and nothing
 * else, which is what keeps React Fast Refresh working on it.
 */

/** The six compositions a card can be drawn in. */
export type IllustrationVariant =
  | "skyline"
  | "offset"
  | "joined"
  | "cropped"
  | "medallion"
  | "horizon";

const VARIANTS: IllustrationVariant[] = [
  "skyline",
  "offset",
  "joined",
  "cropped",
  "medallion",
  "horizon",
];

/** Compositions that still read well with a single landmark in them. */
const SOLO_VARIANTS: IllustrationVariant[] = [
  "offset",
  "cropped",
  "medallion",
  "horizon",
];

/** FNV-1a. Small, stable, and dependency-free; any stable hash would do. */
function hash(value: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < value.length; i++) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/**
 * Same cities, same composition, every time and everywhere.
 *
 * Keyed on the city names rather than the list index. Index would have been
 * simpler and wrong: re-sorting the results by price would redraw every card,
 * and a trip whose picture changes when you sort it does not feel like a
 * place. Keyed on the cities, Prague plus Vienna is drawn the same way
 * wherever it lands - in the list, in compare, in saved trips.
 */
export function variantFor(cities: string[]): IllustrationVariant {
  const key = cities.join(">");
  const pool = cities.length < 2 ? SOLO_VARIANTS : VARIANTS;
  return pool[hash(key) % pool.length];
}

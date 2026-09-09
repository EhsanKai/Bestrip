/* Landmark line art, one entry per city the engine can return.
 *
 * The destination catalog is a closed set of sixteen cities, so every one of
 * them is drawn here by hand rather than approximated by a generator. A
 * procedural "skyline" would have been less work and would have looked it:
 * the whole point of this layer is that Prague reads as Prague.
 *
 * Every landmark is drawn on one shared stage - a 200x100 viewBox with the
 * ground at y=88 - so any two cities can stand side by side without one
 * floating or towering over its neighbour. Paths are stroke-only and use
 * `currentColor`, which is what lets a single drawing sit correctly on ivory
 * in light mode and on navy in dark without a second asset.
 *
 * House style, kept deliberately narrow so sixteen drawings by one hand still
 * look like one hand: thin continuous contours, no fills, no hatching, no
 * interior detail below roughly 2px, and never more than one heavier
 * structural line per landmark.
 */

export const GROUND_Y = 88;
export const STAGE_W = 200;
export const STAGE_H = 100;

export interface Landmark {
  /** Outline paths, drawn at the standard stroke weight. */
  d: string[];
  /** One optional heavier line: the structure that carries the silhouette. */
  anchor?: string;
  /** Natural width on the shared stage, used to place two cities together. */
  width: number;
  /** What a screen reader should be told this drawing is, if anything. */
  label: string;
}

/** Half-round arch: a compact helper, since half the catalog is arcades. */
function arch(x: number, y: number, w: number, h: number): string {
  const r = w / 2;
  return `M${x} ${y}V${y - h + r}A${r} ${r} 0 0 1 ${x + w} ${y - h + r}V${y}`;
}

export const CITY_LANDMARKS: Record<string, Landmark> = {
  Prague: {
    label: "Charles Bridge and the Old Town towers",
    width: 96,
    anchor: `M2 ${GROUND_Y}H94`,
    d: [
      // Bridge arches over the Vltava.
      `M6 ${GROUND_Y}q10 -13 20 0q10 -13 20 0q10 -13 20 0q10 -13 20 0`,
      // Gothic bridge tower, stepped and spired.
      `M28 ${GROUND_Y}V44h18v44`,
      "M28 44l9-13l9 13",
      "M32 44v-8h10v8",
      "M37 31V22",
      // Lesser tower behind, half height.
      `M58 ${GROUND_Y}V52h12v36`,
      "M58 52l6-9l6 9",
      "M64 43v-6",
    ],
  },

  Vienna: {
    label: "St Stephen's Cathedral",
    width: 84,
    anchor: `M4 ${GROUND_Y}H80`,
    d: [
      // Nave with the steep tiled roof.
      `M10 ${GROUND_Y}V58h44v30`,
      "M10 58l22-14l22 14",
      // South tower.
      `M54 ${GROUND_Y}V40h14v48`,
      "M54 40l7-26l7 26",
      "M61 14V6",
      // Rose window and aisle rhythm.
      "M28 70a4 4 0 1 0 8 0a4 4 0 1 0 -8 0",
      `M18 ${GROUND_Y}v-10M46 ${GROUND_Y}v-10`,
    ],
  },

  Berlin: {
    label: "The Brandenburg Gate and the television tower",
    width: 100,
    anchor: `M2 ${GROUND_Y}H98`,
    d: [
      // Brandenburg Gate. Taller than a first pass had it: at gate
      // proportions the entablature and columns read as a table top.
      "M6 54h56",
      "M10 54v6h48v-6",
      // Six columns, evenly spaced under the entablature.
      `M14 60V${GROUND_Y}M22 60V${GROUND_Y}M30 60V${GROUND_Y}M38 60V${GROUND_Y}M46 60V${GROUND_Y}M54 60V${GROUND_Y}`,
      // Attic storey and the quadriga on top.
      "M18 54V46h32v8",
      "M28 46v-7h12v7",
      "M31 39l3-4l3 4",
      // Fernsehturm: shaft, sphere, antenna.
      `M80 ${GROUND_Y}V48M85 ${GROUND_Y}V48`,
      "M74 42a8.5 8.5 0 1 1 17 0a8.5 8.5 0 1 1 -17 0",
      "M82.5 33V16",
      "M80 48h5",
    ],
  },

  Budapest: {
    label: "The Hungarian Parliament on the Danube",
    width: 104,
    anchor: `M2 ${GROUND_Y}H102`,
    d: [
      // Riverfront frontage.
      "M8 78h88v10H8z",
      // Central dome and lantern.
      "M40 78V64a12 12 0 0 1 24 0v14",
      "M52 52V44",
      "M46 64h12",
      // Flanking gothic spires, symmetrical.
      "M20 78V62l6-10l6 10v16",
      "M72 78V62l6-10l6 10v16",
      // Arcade at the water line.
      `M14 88q6 -7 12 0q6 -7 12 0q6 -7 12 0q6 -7 12 0q6 -7 12 0`,
    ],
  },

  Paris: {
    label: "The Eiffel Tower",
    width: 76,
    anchor: `M4 ${GROUND_Y}H72`,
    d: [
      // The two legs, curving in the way the real silhouette does.
      `M22 ${GROUND_Y}q12 -30 14 -62`,
      `M54 ${GROUND_Y}q-12 -30 -14 -62`,
      // Decks.
      "M27 62h22",
      "M31 44h14",
      "M34 30h8",
      // The arch under the first deck, and the mast.
      "M30 62q8 -9 16 0",
      "M38 26V12",
    ],
  },

  Rome: {
    label: "The Colosseum",
    width: 92,
    anchor: `M6 ${GROUND_Y}H86`,
    d: [
      // Elliptical outer wall, broken where the facade has fallen.
      `M10 ${GROUND_Y}V52a36 22 0 0 1 72 0v36`,
      "M10 70h72",
      "M10 60h72",
      // Two arcade tiers.
      arch(18, 70, 12, 10),
      arch(34, 70, 12, 10),
      arch(50, 70, 12, 10),
      arch(66, 70, 12, 10),
      arch(22, 60, 10, 8),
      arch(38, 60, 10, 8),
      arch(54, 60, 10, 8),
    ],
  },

  Amsterdam: {
    label: "Canal houses",
    width: 96,
    anchor: `M4 ${GROUND_Y}H92`,
    d: [
      // A terrace of narrow houses, each with a different gable. The variety
      // is the point: a row of identical ones reads as a barcode.
      `M8 ${GROUND_Y}V56h18v32`,
      "M8 56l9-10l9 10",
      `M26 ${GROUND_Y}V50h16v38`,
      "M26 50h4v-5h8v5h4",
      `M42 ${GROUND_Y}V60h18v28`,
      "M42 60l9-9l9 9",
      `M60 ${GROUND_Y}V54h16v34`,
      "M60 54h5v-6h6v6h5",
      // Windows, one column per house.
      "M15 66h4M32 62h4M49 70h4M66 66h4",
      // Water line.
      "M4 92h88",
    ],
  },

  Barcelona: {
    label: "The Sagrada Familia",
    width: 88,
    anchor: `M6 ${GROUND_Y}H82`,
    d: [
      // Four tapering towers of unequal height.
      `M14 ${GROUND_Y}V50q4 -22 6 -30q2 8 6 30v38`,
      `M32 ${GROUND_Y}V44q4 -26 6 -36q2 10 6 36v44`,
      `M50 ${GROUND_Y}V46q4 -24 6 -33q2 9 6 33v42`,
      `M66 ${GROUND_Y}V54q3 -18 5 -25q2 7 5 25v34`,
      // Perforations, the detail the towers are known for.
      "M18 62h4M36 58h4M54 60h4M69 66h4",
      "M18 72h4M36 68h4M54 70h4",
    ],
  },

  London: {
    label: "Big Ben and the London Eye",
    width: 104,
    anchor: `M2 ${GROUND_Y}H102`,
    d: [
      // Elizabeth Tower.
      `M14 ${GROUND_Y}V34h16v54`,
      "M14 34h16",
      "M16 34l6-12l6 12",
      "M22 22V14",
      // Clock face.
      "M17 44a5 5 0 1 0 10 0a5 5 0 1 0 -10 0",
      // Parliament frontage.
      `M30 ${GROUND_Y}V66h26v22`,
      "M36 66v-8M44 66v-8M52 66v-8",
      // The Eye.
      "M60 56a22 22 0 1 0 44 0a22 22 0 1 0 -44 0",
      "M82 34V78",
      "M60 56h44",
      "M66 40l32 32M98 40l-32 32",
      `M78 78l4 10M86 78l-4 10`,
    ],
  },

  Madrid: {
    label: "The Metropolis building",
    width: 84,
    anchor: `M6 ${GROUND_Y}H78`,
    d: [
      // Corner rotunda with its cupola.
      `M28 ${GROUND_Y}V56h24v32`,
      "M28 56a12 12 0 0 1 24 0",
      "M40 44V34",
      "M36 34a4 4 0 0 1 8 0",
      // Colonnade.
      `M32 ${GROUND_Y}v-16M40 ${GROUND_Y}v-16M48 ${GROUND_Y}v-16`,
      // Flanking wings.
      `M12 ${GROUND_Y}V66h16v22`,
      `M52 ${GROUND_Y}V66h16v22`,
      "M16 74h8M56 74h8",
    ],
  },

  Milan: {
    label: "The Duomo",
    width: 96,
    anchor: `M4 ${GROUND_Y}H92`,
    d: [
      // Marble facade, wide and low, under a forest of pinnacles.
      `M10 ${GROUND_Y}V64h76v24`,
      "M10 64l38-16l38 16",
      // Pinnacles across the roofline.
      "M20 64V54l3-6l3 6v10",
      "M34 64V50l3-8l3 8v14",
      "M60 64V50l3-8l3 8v14",
      "M74 64V54l3-6l3 6v10",
      // Central spire with the Madonnina.
      "M45 48V34l3-10l3 10v14",
      "M48 22V16",
      // Portals.
      arch(28, 88, 12, 12),
      arch(56, 88, 12, 12),
      arch(42, 88, 12, 16),
    ],
  },

  Munich: {
    label: "The Frauenkirche",
    width: 80,
    anchor: `M8 ${GROUND_Y}H74`,
    d: [
      // Brick nave.
      `M22 ${GROUND_Y}V62h38v26`,
      "M22 62l19-10l19 10",
      // The twin towers with their onion domes.
      `M14 ${GROUND_Y}V44h16v44`,
      "M14 44q8 -16 16 0",
      "M22 28V22",
      `M52 ${GROUND_Y}V44h16v44`,
      "M52 44q8 -16 16 0",
      "M60 28V22",
      // Clock faces.
      "M19 52h6M57 52h6",
    ],
  },

  Copenhagen: {
    label: "Nyhavn and a spired tower",
    width: 92,
    anchor: `M4 ${GROUND_Y}H88`,
    d: [
      // Harbour-front houses.
      `M8 ${GROUND_Y}V62h14v26M22 ${GROUND_Y}V56h14v32M36 ${GROUND_Y}V64h14v24`,
      "M8 62l7-8l7 8M22 56l7-8l7 8M36 64l7-8l7 8",
      "M13 72h4M27 68h4M41 74h4",
      // The twisted spire of Vor Frelsers Kirke.
      `M60 ${GROUND_Y}V56h16v32`,
      "M60 56l8-8l8 8",
      "M68 48q6 -6 0 -10q-6 -5 0 -10",
      "M68 28V20",
      // Water.
      "M4 92h84",
    ],
  },

  Dublin: {
    label: "The Ha'penny Bridge and Georgian houses",
    width: 92,
    anchor: `M4 ${GROUND_Y}H88`,
    d: [
      // The cast-iron footbridge.
      `M12 ${GROUND_Y}V72q26 -22 52 0v16`,
      "M12 72q26 -22 52 0",
      "M20 78v-4M30 74v-4M40 71v-3M50 74v-4M60 78v-4",
      // Georgian terrace behind.
      `M66 ${GROUND_Y}V58h20v30`,
      "M66 58h20",
      "M70 66h4M78 66h4M70 76h4M78 76h4",
      "M4 92h84",
    ],
  },

  Brussels: {
    label: "A Grand Place guildhall and the Atomium",
    width: 96,
    anchor: `M4 ${GROUND_Y}H92`,
    d: [
      // Guildhall with its stepped gable and belfry.
      `M10 ${GROUND_Y}V56h30v32`,
      "M10 56h6v-6h6v-6h6v6h6v6h6",
      `M25 44V30`,
      "M22 30l3-8l3 8",
      "M16 66h6M28 66h6M16 76h6M28 76h6",
      // Atomium: spheres on their struts, reduced to five.
      "M62 44a5 5 0 1 0 10 0a5 5 0 1 0 -10 0",
      "M50 62a5 5 0 1 0 10 0a5 5 0 1 0 -10 0",
      "M74 62a5 5 0 1 0 10 0a5 5 0 1 0 -10 0",
      "M62 80a5 5 0 1 0 10 0a5 5 0 1 0 -10 0",
      "M67 49v26M60 55l7 5M74 55l-7 5M60 69l7-5M74 69l-7-5",
    ],
  },

  Zurich: {
    label: "The Grossmunster towers",
    width: 84,
    anchor: `M6 ${GROUND_Y}H78`,
    d: [
      // Twin Romanesque towers.
      `M16 ${GROUND_Y}V46h16v42`,
      "M16 46h16",
      "M18 46V38h12v8",
      "M20 34h8v4h-8z",
      `M46 ${GROUND_Y}V46h16v42`,
      "M46 46h16",
      "M48 46V38h12v8",
      "M50 34h8v4h-8z",
      // Nave between them.
      `M32 ${GROUND_Y}V66h14v22`,
      "M32 66l7-6l7 6",
      // Clock faces.
      "M21 56h6M51 56h6",
      // Limmat.
      "M6 92h72",
    ],
  },
};

/**
 * A European street wall, used when a city has no drawing of its own.
 *
 * Deliberately generic and deliberately not empty: a card with no illustration
 * would look broken next to five cards that have one, and the fallback's job
 * is to keep the composition intact rather than to depict anywhere.
 */
export const GENERIC_LANDMARK: Landmark = {
  label: "European rooftops",
  width: 88,
  anchor: `M6 ${GROUND_Y}H82`,
  d: [
    `M10 ${GROUND_Y}V62h16v26`,
    "M10 62l8-8l8 8",
    `M26 ${GROUND_Y}V54h18v34`,
    "M26 54l9-9l9 9",
    `M44 ${GROUND_Y}V66h14v22`,
    "M44 66l7-7l7 7",
    `M58 ${GROUND_Y}V58h20v30`,
    "M58 58l10-9l10 9",
    "M16 72h4M33 66h4M49 76h4M66 70h4",
  ],
};

export function landmarkFor(city: string): Landmark {
  return CITY_LANDMARKS[city] ?? GENERIC_LANDMARK;
}

export function hasLandmark(city: string): boolean {
  return city in CITY_LANDMARKS;
}

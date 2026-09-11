"""The ~200-destination European discovery catalog (V9 Phase 2).

The 16 hand-tuned "core" cities keep their full attribute profiles and their
synthetic transport links (``destinations.py``). This module adds the rest of
Europe as broadly-tagged discovery markets: real airport, real ISO country
code, real coordinates, a subregion for the diversity adjustment, and a small
set of **broad, truthful tags** — never fabricated fine-grained personality
scores (V9 §17). The 12 experience attributes for these entries are derived
from the tags by :func:`profile_from_tags`.

These entries are ``acquisition_eligible`` but have **no** synthetic transport
links — they become reachable only through real (or fixture) bounded
acquisition, which is the whole point of the Phase 2 architecture.
"""

from __future__ import annotations

from ..models.destination import Destination

# ---------------------------------------------------------------------------
# Tag -> partial experience profile. A tag nudges the attributes it implies
# toward a high value; the final attribute is the max over all matching tags,
# floored at 0.4 (a discovery city is "average" on a dimension nothing says
# anything about, not "poor"). Deliberately coarse.
# ---------------------------------------------------------------------------
_TAG_PROFILE: dict[str, dict[str, float]] = {
    "history":       {"history": 0.85, "culture": 0.7, "museums": 0.65},
    "ancient":       {"history": 0.95, "architecture": 0.85, "culture": 0.75},
    "culture":       {"culture": 0.85, "museums": 0.7},
    "art":           {"culture": 0.8, "museums": 0.85, "architecture": 0.7},
    "architecture":  {"architecture": 0.85, "history": 0.6},
    "oldtown":       {"history": 0.8, "architecture": 0.8, "romance": 0.65},
    "capital":       {"culture": 0.75, "museums": 0.7, "nightlife": 0.65, "shopping": 0.65},
    "food":          {"food": 0.9},
    "wine":          {"food": 0.8, "nature": 0.55, "romance": 0.6},
    "nightlife":     {"nightlife": 0.9},
    "party":         {"nightlife": 0.95, "adventure": 0.5},
    "beach":         {"beaches": 0.9, "nature": 0.55, "family_friendly": 0.65},
    "coast":         {"beaches": 0.75, "nature": 0.65, "romance": 0.6},
    "island":        {"beaches": 0.85, "nature": 0.7, "adventure": 0.6, "romance": 0.7},
    "nature":        {"nature": 0.9, "adventure": 0.65},
    "mountains":     {"nature": 0.9, "adventure": 0.85},
    "outdoors":      {"nature": 0.8, "adventure": 0.8, "family_friendly": 0.6},
    "spa":           {"nature": 0.6, "romance": 0.7, "family_friendly": 0.6},
    "romance":       {"romance": 0.85},
    "family":        {"family_friendly": 0.9},
    "shopping":      {"shopping": 0.85},
    "design":        {"architecture": 0.7, "shopping": 0.75, "culture": 0.7},
    "modern":        {"architecture": 0.7, "nightlife": 0.65, "shopping": 0.7},
    "student":       {"nightlife": 0.8, "culture": 0.65, "food": 0.6},
    "budget":        {},   # a pure economic tag — no experience implication
    "hipster":       {"nightlife": 0.75, "food": 0.7, "culture": 0.65},
    "seaside":       {"beaches": 0.8, "nature": 0.6, "family_friendly": 0.65},
    "port":          {"history": 0.6, "food": 0.65, "culture": 0.6},
    "medieval":      {"history": 0.9, "architecture": 0.85},
    "baroque":       {"architecture": 0.85, "history": 0.75, "culture": 0.7},
    "cathedral":     {"architecture": 0.8, "history": 0.75},
    "lakes":         {"nature": 0.85, "romance": 0.7, "family_friendly": 0.65},
    "fjords":        {"nature": 0.95, "adventure": 0.8},
    "northern-lights": {"nature": 0.85, "adventure": 0.75, "romance": 0.7},
    "winter":        {"adventure": 0.8, "nature": 0.7, "family_friendly": 0.65},
    "festival":      {"nightlife": 0.8, "culture": 0.75},
    "riverside":     {"romance": 0.65, "nature": 0.6, "architecture": 0.6},
}

_FLOOR = 0.4


def profile_from_tags(tags: tuple[str, ...]) -> dict[str, float]:
    """Derive the 12 experience attributes from broad tags. Coarse by design —
    max over the tags that imply each attribute, floored at 0.4."""
    from ..models.destination import EXPERIENCE_ATTRIBUTES

    out = {a: _FLOOR for a in EXPERIENCE_ATTRIBUTES}
    out["beaches"] = 0.0  # unlike the others, absence of coast really is 0
    for t in tags:
        for attr, val in _TAG_PROFILE.get(t, {}).items():
            out[attr] = max(out[attr], val)
    return out


# ---------------------------------------------------------------------------
# The catalogue. Columns:
#   id | country | cc | subregion | airport | (lat, lon) | tz | tags | (min,max) days
# id doubles as the display name. Coordinates are the city centre (rounded).
# ---------------------------------------------------------------------------
_C = tuple[str, str, str, str, str, tuple[float, float], str, tuple[str, ...], tuple[float, float]]

_CATALOG: tuple[_C, ...] = (
    # --- Iberia ---------------------------------------------------------
    ("Lisbon", "Portugal", "PT", "Iberia", "LIS", (38.72, -9.14), "Europe/Lisbon", ("capital", "history", "food", "coast", "nightlife"), (2.0, 5.0)),
    ("Porto", "Portugal", "PT", "Iberia", "OPO", (41.15, -8.61), "Europe/Lisbon", ("oldtown", "wine", "food", "riverside", "budget"), (2.0, 4.0)),
    ("Faro", "Portugal", "PT", "Iberia", "FAO", (37.02, -7.93), "Europe/Lisbon", ("beach", "coast", "seaside", "budget"), (2.0, 5.0)),
    ("Seville", "Spain", "ES", "Iberia", "SVQ", (37.39, -5.99), "Europe/Madrid", ("history", "culture", "food", "oldtown", "festival"), (2.0, 4.0)),
    ("Valencia", "Spain", "ES", "Iberia", "VLC", (39.47, -0.38), "Europe/Madrid", ("beach", "food", "modern", "family", "budget"), (2.0, 4.0)),
    ("Malaga", "Spain", "ES", "Iberia", "AGP", (36.72, -4.42), "Europe/Madrid", ("beach", "art", "coast", "budget"), (2.0, 5.0)),
    ("Bilbao", "Spain", "ES", "Iberia", "BIO", (43.26, -2.93), "Europe/Madrid", ("art", "food", "modern", "design"), (2.0, 3.0)),
    ("Palma de Mallorca", "Spain", "ES", "Iberia", "PMI", (39.57, 2.65), "Europe/Madrid", ("island", "beach", "oldtown", "party"), (3.0, 7.0)),
    ("Ibiza", "Spain", "ES", "Iberia", "IBZ", (38.91, 1.43), "Europe/Madrid", ("island", "party", "beach", "nightlife"), (3.0, 6.0)),
    ("Granada", "Spain", "ES", "Iberia", "GRX", (37.18, -3.60), "Europe/Madrid", ("history", "ancient", "culture", "budget"), (2.0, 3.0)),
    ("Alicante", "Spain", "ES", "Iberia", "ALC", (38.35, -0.48), "Europe/Madrid", ("beach", "seaside", "budget", "family"), (3.0, 6.0)),
    ("San Sebastian", "Spain", "ES", "Iberia", "EAS", (43.32, -1.98), "Europe/Madrid", ("food", "beach", "coast", "romance"), (2.0, 4.0)),
    ("Santiago de Compostela", "Spain", "ES", "Iberia", "SCQ", (42.88, -8.54), "Europe/Madrid", ("history", "cathedral", "oldtown", "culture"), (2.0, 3.0)),
    ("Tenerife", "Spain", "ES", "Iberia", "TFS", (28.05, -16.57), "Atlantic/Canary", ("island", "beach", "mountains", "outdoors"), (4.0, 8.0)),
    ("Las Palmas", "Spain", "ES", "Iberia", "LPA", (28.10, -15.42), "Atlantic/Canary", ("island", "beach", "seaside", "budget"), (4.0, 8.0)),

    # --- France ------------------------------------------------------
    ("Nice", "France", "FR", "France", "NCE", (43.70, 7.27), "Europe/Paris", ("coast", "beach", "art", "romance"), (2.0, 5.0)),
    ("Lyon", "France", "FR", "France", "LYS", (45.76, 4.83), "Europe/Paris", ("food", "history", "oldtown", "wine"), (2.0, 4.0)),
    ("Marseille", "France", "FR", "France", "MRS", (43.30, 5.37), "Europe/Paris", ("port", "coast", "food", "budget"), (2.0, 4.0)),
    ("Bordeaux", "France", "FR", "France", "BOD", (44.84, -0.58), "Europe/Paris", ("wine", "food", "architecture", "riverside"), (2.0, 4.0)),
    ("Toulouse", "France", "FR", "France", "TLS", (43.60, 1.44), "Europe/Paris", ("history", "student", "food", "riverside"), (2.0, 3.0)),
    ("Nantes", "France", "FR", "France", "NTE", (47.22, -1.55), "Europe/Paris", ("culture", "art", "riverside", "modern"), (2.0, 3.0)),
    ("Strasbourg", "France", "FR", "France", "SXB", (48.58, 7.75), "Europe/Paris", ("oldtown", "history", "cathedral", "riverside"), (2.0, 3.0)),
    ("Biarritz", "France", "FR", "France", "BIQ", (43.48, -1.56), "Europe/Paris", ("beach", "coast", "seaside", "romance"), (2.0, 5.0)),
    ("Ajaccio", "France", "FR", "France", "AJA", (41.93, 8.74), "Europe/Paris", ("island", "beach", "nature", "history"), (3.0, 7.0)),

    # --- Italy ------------------------------------------------------
    ("Venice", "Italy", "IT", "Italy", "VCE", (45.44, 12.34), "Europe/Rome", ("history", "romance", "art", "oldtown"), (2.0, 4.0)),
    ("Florence", "Italy", "IT", "Italy", "FLR", (43.77, 11.26), "Europe/Rome", ("art", "history", "architecture", "food"), (2.0, 4.0)),
    ("Naples", "Italy", "IT", "Italy", "NAP", (40.85, 14.27), "Europe/Rome", ("history", "food", "port", "budget"), (2.0, 4.0)),
    ("Bologna", "Italy", "IT", "Italy", "BLQ", (44.49, 11.34), "Europe/Rome", ("food", "student", "history", "oldtown"), (2.0, 3.0)),
    ("Turin", "Italy", "IT", "Italy", "TRN", (45.07, 7.69), "Europe/Rome", ("architecture", "food", "baroque", "culture"), (2.0, 3.0)),
    ("Verona", "Italy", "IT", "Italy", "VRN", (45.44, 10.99), "Europe/Rome", ("romance", "history", "ancient", "oldtown"), (1.0, 3.0)),
    ("Palermo", "Italy", "IT", "Italy", "PMO", (38.12, 13.36), "Europe/Rome", ("history", "food", "port", "budget"), (2.0, 4.0)),
    ("Catania", "Italy", "IT", "Italy", "CTA", (37.50, 15.09), "Europe/Rome", ("history", "coast", "outdoors", "budget"), (2.0, 4.0)),
    ("Cagliari", "Italy", "IT", "Italy", "CAG", (39.22, 9.12), "Europe/Rome", ("island", "beach", "history", "coast"), (3.0, 6.0)),
    ("Bari", "Italy", "IT", "Italy", "BRI", (41.13, 16.87), "Europe/Rome", ("oldtown", "coast", "food", "budget"), (2.0, 3.0)),
    ("Pisa", "Italy", "IT", "Italy", "PSA", (43.72, 10.40), "Europe/Rome", ("history", "architecture", "student"), (1.0, 2.0)),

    # --- Central Europe -------------------------------------------------
    ("Frankfurt", "Germany", "DE", "Central Europe", "FRA", (50.11, 8.68), "Europe/Berlin", ("modern", "shopping", "riverside", "food"), (1.0, 3.0)),
    ("Hamburg", "Germany", "DE", "Central Europe", "HAM", (53.55, 10.00), "Europe/Berlin", ("port", "nightlife", "modern", "culture"), (2.0, 4.0)),
    ("Cologne", "Germany", "DE", "Central Europe", "CGN", (50.94, 6.96), "Europe/Berlin", ("cathedral", "nightlife", "history", "riverside"), (2.0, 3.0)),
    ("Dusseldorf", "Germany", "DE", "Central Europe", "DUS", (51.23, 6.78), "Europe/Berlin", ("shopping", "design", "riverside", "nightlife"), (1.0, 3.0)),
    ("Stuttgart", "Germany", "DE", "Central Europe", "STR", (48.78, 9.18), "Europe/Berlin", ("modern", "wine", "culture", "family"), (1.0, 3.0)),
    ("Nuremberg", "Germany", "DE", "Central Europe", "NUE", (49.45, 11.08), "Europe/Berlin", ("medieval", "history", "oldtown", "family"), (2.0, 3.0)),
    ("Leipzig", "Germany", "DE", "Central Europe", "LEJ", (51.34, 12.37), "Europe/Berlin", ("culture", "art", "student", "budget"), (2.0, 3.0)),
    ("Dresden", "Germany", "DE", "Central Europe", "DRS", (51.05, 13.74), "Europe/Berlin", ("baroque", "art", "history", "riverside"), (2.0, 3.0)),
    ("Bremen", "Germany", "DE", "Central Europe", "BRE", (53.08, 8.80), "Europe/Berlin", ("medieval", "oldtown", "history", "family"), (1.0, 2.0)),
    ("Hannover", "Germany", "DE", "Central Europe", "HAJ", (52.38, 9.73), "Europe/Berlin", ("culture", "family", "modern"), (1.0, 2.0)),
    ("Salzburg", "Austria", "AT", "Central Europe", "SZG", (47.80, 13.05), "Europe/Vienna", ("baroque", "history", "oldtown", "romance"), (2.0, 3.0)),
    ("Innsbruck", "Austria", "AT", "Central Europe", "INN", (47.27, 11.39), "Europe/Vienna", ("mountains", "winter", "outdoors", "history"), (2.0, 4.0)),
    ("Graz", "Austria", "AT", "Central Europe", "GRZ", (47.07, 15.44), "Europe/Vienna", ("oldtown", "student", "culture", "budget"), (2.0, 3.0)),
    ("Geneva", "Switzerland", "CH", "Central Europe", "GVA", (46.20, 6.14), "Europe/Zurich", ("lakes", "nature", "modern", "culture"), (1.0, 3.0)),
    ("Basel", "Switzerland", "CH", "Central Europe", "BSL", (47.56, 7.59), "Europe/Zurich", ("art", "riverside", "architecture", "culture"), (1.0, 3.0)),
    ("Bern", "Switzerland", "CH", "Central Europe", "BRN", (46.95, 7.45), "Europe/Zurich", ("oldtown", "medieval", "riverside", "history"), (1.0, 2.0)),
    ("Ljubljana", "Slovenia", "SI", "Central Europe", "LJU", (46.06, 14.51), "Europe/Ljubljana", ("oldtown", "riverside", "nature", "budget"), (2.0, 3.0)),
    ("Bratislava", "Slovakia", "SK", "Central Europe", "BTS", (48.15, 17.11), "Europe/Bratislava", ("oldtown", "history", "budget", "riverside"), (1.0, 2.0)),
    ("Krakow", "Poland", "PL", "Central Europe", "KRK", (50.06, 19.94), "Europe/Warsaw", ("history", "oldtown", "culture", "budget"), (2.0, 4.0)),
    ("Warsaw", "Poland", "PL", "Central Europe", "WAW", (52.23, 21.01), "Europe/Warsaw", ("capital", "history", "modern", "budget"), (2.0, 4.0)),
    ("Wroclaw", "Poland", "PL", "Central Europe", "WRO", (51.11, 17.04), "Europe/Warsaw", ("oldtown", "riverside", "student", "budget"), (2.0, 3.0)),
    ("Gdansk", "Poland", "PL", "Central Europe", "GDN", (54.35, 18.65), "Europe/Warsaw", ("oldtown", "history", "port", "budget"), (2.0, 3.0)),
    ("Poznan", "Poland", "PL", "Central Europe", "POZ", (52.41, 16.93), "Europe/Warsaw", ("oldtown", "history", "budget", "student"), (1.0, 3.0)),

    # --- Balkans -----------------------------------------------------
    ("Zagreb", "Croatia", "HR", "Balkans", "ZAG", (45.81, 15.98), "Europe/Zagreb", ("oldtown", "culture", "budget", "food"), (2.0, 3.0)),
    ("Split", "Croatia", "HR", "Balkans", "SPU", (43.51, 16.44), "Europe/Zagreb", ("ancient", "beach", "coast", "history"), (2.0, 5.0)),
    ("Dubrovnik", "Croatia", "HR", "Balkans", "DBV", (42.65, 18.09), "Europe/Zagreb", ("oldtown", "coast", "history", "romance"), (2.0, 4.0)),
    ("Zadar", "Croatia", "HR", "Balkans", "ZAD", (44.12, 15.23), "Europe/Zagreb", ("coast", "history", "seaside", "budget"), (2.0, 4.0)),
    ("Belgrade", "Serbia", "RS", "Balkans", "BEG", (44.79, 20.45), "Europe/Belgrade", ("nightlife", "party", "history", "budget"), (2.0, 4.0)),
    ("Sofia", "Bulgaria", "BG", "Balkans", "SOF", (42.70, 23.32), "Europe/Sofia", ("history", "mountains", "budget", "culture"), (2.0, 3.0)),
    ("Plovdiv", "Bulgaria", "BG", "Balkans", "PDV", (42.14, 24.75), "Europe/Sofia", ("ancient", "oldtown", "art", "budget"), (2.0, 3.0)),
    ("Bucharest", "Romania", "RO", "Balkans", "OTP", (44.43, 26.10), "Europe/Bucharest", ("capital", "nightlife", "architecture", "budget"), (2.0, 4.0)),
    ("Cluj-Napoca", "Romania", "RO", "Balkans", "CLJ", (46.77, 23.60), "Europe/Bucharest", ("student", "nightlife", "festival", "budget"), (2.0, 3.0)),
    ("Sarajevo", "Bosnia and Herzegovina", "BA", "Balkans", "SJJ", (43.86, 18.41), "Europe/Sarajevo", ("history", "oldtown", "culture", "budget"), (2.0, 3.0)),
    ("Podgorica", "Montenegro", "ME", "Balkans", "TGD", (42.44, 19.26), "Europe/Podgorica", ("nature", "mountains", "budget", "coast"), (2.0, 4.0)),
    ("Tirana", "Albania", "AL", "Balkans", "TIA", (41.33, 19.82), "Europe/Tirane", ("budget", "nightlife", "coast", "history"), (2.0, 4.0)),
    ("Skopje", "North Macedonia", "MK", "Balkans", "SKP", (41.99, 21.43), "Europe/Skopje", ("history", "budget", "riverside", "culture"), (1.0, 3.0)),
    ("Pristina", "Kosovo", "XK", "Balkans", "PRN", (42.66, 21.16), "Europe/Belgrade", ("budget", "history", "student"), (1.0, 2.0)),
    ("Thessaloniki", "Greece", "GR", "Balkans", "SKG", (40.64, 22.94), "Europe/Athens", ("history", "food", "seaside", "student"), (2.0, 4.0)),

    # --- Greece / Mediterranean islands -------------------------------
    ("Athens", "Greece", "GR", "Greece", "ATH", (37.98, 23.73), "Europe/Athens", ("ancient", "history", "capital", "food"), (2.0, 4.0)),
    ("Heraklion", "Greece", "GR", "Greece", "HER", (35.34, 25.13), "Europe/Athens", ("island", "beach", "ancient", "history"), (3.0, 7.0)),
    ("Rhodes", "Greece", "GR", "Greece", "RHO", (36.43, 28.22), "Europe/Athens", ("island", "medieval", "beach", "history"), (3.0, 7.0)),
    ("Corfu", "Greece", "GR", "Greece", "CFU", (39.62, 19.92), "Europe/Athens", ("island", "beach", "oldtown", "nature"), (3.0, 7.0)),
    ("Chania", "Greece", "GR", "Greece", "CHQ", (35.51, 24.02), "Europe/Athens", ("island", "oldtown", "beach", "romance"), (3.0, 6.0)),
    ("Santorini", "Greece", "GR", "Greece", "JTR", (36.39, 25.46), "Europe/Athens", ("island", "romance", "coast", "wine"), (2.0, 5.0)),
    ("Mykonos", "Greece", "GR", "Greece", "JMK", (37.45, 25.35), "Europe/Athens", ("island", "party", "beach", "nightlife"), (2.0, 5.0)),
    ("Kos", "Greece", "GR", "Greece", "KGS", (36.79, 27.09), "Europe/Athens", ("island", "beach", "ancient", "family"), (4.0, 7.0)),
    ("Valletta", "Malta", "MT", "Greece", "MLA", (35.90, 14.51), "Europe/Malta", ("history", "oldtown", "baroque", "coast"), (2.0, 4.0)),
    ("Larnaca", "Cyprus", "CY", "Greece", "LCA", (34.92, 33.62), "Asia/Nicosia", ("beach", "coast", "ancient", "seaside"), (3.0, 7.0)),
    ("Paphos", "Cyprus", "CY", "Greece", "PFO", (34.77, 32.42), "Asia/Nicosia", ("beach", "ancient", "coast", "family"), (3.0, 7.0)),

    # --- Nordics ---------------------------------------------------
    ("Stockholm", "Sweden", "SE", "Nordics", "ARN", (59.33, 18.07), "Europe/Stockholm", ("design", "capital", "history", "modern"), (2.0, 4.0)),
    ("Gothenburg", "Sweden", "SE", "Nordics", "GOT", (57.71, 11.97), "Europe/Stockholm", ("seaside", "food", "design", "family"), (2.0, 3.0)),
    ("Malmo", "Sweden", "SE", "Nordics", "MMX", (55.60, 13.00), "Europe/Stockholm", ("modern", "design", "riverside", "budget"), (1.0, 3.0)),
    ("Oslo", "Norway", "NO", "Nordics", "OSL", (59.91, 10.75), "Europe/Oslo", ("capital", "design", "nature", "modern"), (2.0, 4.0)),
    ("Bergen", "Norway", "NO", "Nordics", "BGO", (60.39, 5.32), "Europe/Oslo", ("fjords", "nature", "outdoors", "port"), (2.0, 4.0)),
    ("Tromso", "Norway", "NO", "Nordics", "TOS", (69.65, 18.96), "Europe/Oslo", ("northern-lights", "winter", "outdoors", "nature"), (3.0, 5.0)),
    ("Helsinki", "Finland", "FI", "Nordics", "HEL", (60.17, 24.94), "Europe/Helsinki", ("design", "capital", "seaside", "modern"), (2.0, 4.0)),
    ("Rovaniemi", "Finland", "FI", "Nordics", "RVN", (66.50, 25.73), "Europe/Helsinki", ("winter", "northern-lights", "family", "outdoors"), (3.0, 5.0)),
    ("Reykjavik", "Iceland", "IS", "Nordics", "KEF", (64.15, -21.94), "Atlantic/Reykjavik", ("northern-lights", "nature", "outdoors", "spa"), (3.0, 6.0)),
    ("Aarhus", "Denmark", "DK", "Nordics", "AAR", (56.16, 10.20), "Europe/Copenhagen", ("design", "student", "art", "seaside"), (2.0, 3.0)),
    ("Billund", "Denmark", "DK", "Nordics", "BLL", (55.73, 9.11), "Europe/Copenhagen", ("family", "outdoors"), (2.0, 3.0)),

    # --- Baltics ---------------------------------------------------
    ("Tallinn", "Estonia", "EE", "Baltics", "TLL", (59.44, 24.75), "Europe/Tallinn", ("medieval", "oldtown", "history", "budget"), (2.0, 3.0)),
    ("Riga", "Latvia", "LV", "Baltics", "RIX", (56.95, 24.11), "Europe/Riga", ("oldtown", "architecture", "nightlife", "budget"), (2.0, 3.0)),
    ("Vilnius", "Lithuania", "LT", "Baltics", "VNO", (54.69, 25.28), "Europe/Vilnius", ("baroque", "oldtown", "history", "budget"), (2.0, 3.0)),
    ("Kaunas", "Lithuania", "LT", "Baltics", "KUN", (54.90, 23.90), "Europe/Vilnius", ("oldtown", "art", "modern", "budget"), (1.0, 3.0)),

    # --- UK & Ireland ----------------------------------------------
    ("Edinburgh", "United Kingdom", "GB", "UK & Ireland", "EDI", (55.95, -3.19), "Europe/London", ("history", "oldtown", "festival", "culture"), (2.0, 4.0)),
    ("Manchester", "United Kingdom", "GB", "UK & Ireland", "MAN", (53.48, -2.24), "Europe/London", ("nightlife", "modern", "student", "shopping"), (2.0, 3.0)),
    ("Glasgow", "United Kingdom", "GB", "UK & Ireland", "GLA", (55.86, -4.25), "Europe/London", ("nightlife", "art", "modern", "student"), (2.0, 3.0)),
    ("Liverpool", "United Kingdom", "GB", "UK & Ireland", "LPL", (53.41, -2.99), "Europe/London", ("culture", "port", "nightlife", "budget"), (2.0, 3.0)),
    ("Birmingham", "United Kingdom", "GB", "UK & Ireland", "BHX", (52.48, -1.90), "Europe/London", ("modern", "shopping", "family", "budget"), (1.0, 3.0)),
    ("Bristol", "United Kingdom", "GB", "UK & Ireland", "BRS", (51.45, -2.59), "Europe/London", ("hipster", "art", "riverside", "student"), (2.0, 3.0)),
    ("Newcastle", "United Kingdom", "GB", "UK & Ireland", "NCL", (54.98, -1.61), "Europe/London", ("nightlife", "riverside", "budget", "modern"), (2.0, 3.0)),
    ("Belfast", "United Kingdom", "GB", "UK & Ireland", "BFS", (54.60, -5.93), "Europe/London", ("history", "port", "culture", "budget"), (2.0, 3.0)),
    ("Cork", "Ireland", "IE", "UK & Ireland", "ORK", (51.90, -8.47), "Europe/Dublin", ("food", "port", "riverside", "budget"), (2.0, 3.0)),

    # --- Benelux / Netherlands ------------------------------------------
    ("Rotterdam", "Netherlands", "NL", "Benelux", "RTM", (51.92, 4.48), "Europe/Amsterdam", ("modern", "architecture", "design", "port"), (1.0, 3.0)),
    ("Eindhoven", "Netherlands", "NL", "Benelux", "EIN", (51.44, 5.48), "Europe/Amsterdam", ("design", "modern", "budget", "student"), (1.0, 2.0)),
    ("Maastricht", "Netherlands", "NL", "Benelux", "MST", (50.85, 5.69), "Europe/Amsterdam", ("oldtown", "food", "riverside", "history"), (1.0, 3.0)),
    ("Luxembourg", "Luxembourg", "LU", "Benelux", "LUX", (49.61, 6.13), "Europe/Luxembourg", ("history", "oldtown", "modern", "cathedral"), (1.0, 2.0)),
    ("Antwerp", "Belgium", "BE", "Benelux", "ANR", (51.22, 4.40), "Europe/Brussels", ("design", "art", "shopping", "food"), (1.0, 3.0)),
    ("Charleroi", "Belgium", "BE", "Benelux", "CRL", (50.41, 4.44), "Europe/Brussels", ("budget", "modern"), (1.0, 2.0)),
    ("Groningen", "Netherlands", "NL", "Benelux", "GRQ", (53.22, 6.57), "Europe/Amsterdam", ("student", "riverside", "budget", "culture"), (1.0, 2.0)),

    # --- Central / Eastern (rest) --------------------------------------
    ("Brno", "Czechia", "CZ", "Central Europe", "BRQ", (49.20, 16.61), "Europe/Prague", ("student", "modern", "budget", "culture"), (1.0, 3.0)),
    ("Kyiv", "Ukraine", "UA", "Central Europe", "IEV", (50.45, 30.52), "Europe/Kyiv", ("history", "architecture", "budget", "culture"), (2.0, 4.0)),
    ("Chisinau", "Moldova", "MD", "Central Europe", "KIV", (47.01, 28.86), "Europe/Chisinau", ("wine", "budget", "culture"), (1.0, 3.0)),
    ("Debrecen", "Hungary", "HU", "Central Europe", "DEB", (47.53, 21.63), "Europe/Budapest", ("spa", "student", "budget", "festival"), (1.0, 3.0)),

    # --- Turkey (European gateway) ------------------------------------
    ("Istanbul", "Turkey", "TR", "Anatolia", "IST", (41.01, 28.98), "Europe/Istanbul", ("history", "ancient", "food", "shopping", "budget"), (3.0, 5.0)),
    ("Izmir", "Turkey", "TR", "Anatolia", "ADB", (38.42, 27.14), "Europe/Istanbul", ("coast", "ancient", "food", "budget"), (2.0, 4.0)),
    ("Antalya", "Turkey", "TR", "Anatolia", "AYT", (36.90, 30.70), "Europe/Istanbul", ("beach", "ancient", "coast", "family"), (4.0, 8.0)),

    # --- Batch 2: secondary cities with their own airports ------------
    ("Girona", "Spain", "ES", "Iberia", "GRO", (41.98, 2.82), "Europe/Madrid", ("oldtown", "history", "food", "budget"), (1.0, 3.0)),
    ("Santander", "Spain", "ES", "Iberia", "SDR", (43.46, -3.80), "Europe/Madrid", ("beach", "coast", "seaside", "food"), (2.0, 4.0)),
    ("Vigo", "Spain", "ES", "Iberia", "VGO", (42.24, -8.72), "Europe/Madrid", ("port", "seaside", "food", "budget"), (2.0, 3.0)),
    ("Asturias", "Spain", "ES", "Iberia", "OVD", (43.36, -5.85), "Europe/Madrid", ("nature", "coast", "food", "outdoors"), (2.0, 4.0)),
    ("Murcia", "Spain", "ES", "Iberia", "RMU", (37.99, -1.13), "Europe/Madrid", ("history", "food", "budget", "seaside"), (2.0, 3.0)),
    ("Funchal", "Portugal", "PT", "Iberia", "FNC", (32.65, -16.91), "Atlantic/Madeira", ("island", "nature", "outdoors", "wine"), (4.0, 8.0)),
    ("Ponta Delgada", "Portugal", "PT", "Iberia", "PDL", (37.74, -25.67), "Atlantic/Azores", ("island", "nature", "outdoors", "lakes"), (4.0, 7.0)),
    ("Montpellier", "France", "FR", "France", "MPL", (43.61, 3.88), "Europe/Paris", ("student", "history", "coast", "culture"), (2.0, 3.0)),
    ("Lille", "France", "FR", "France", "LIL", (50.63, 3.06), "Europe/Paris", ("oldtown", "student", "art", "food"), (1.0, 3.0)),
    ("Rennes", "France", "FR", "France", "RNS", (48.11, -1.68), "Europe/Paris", ("oldtown", "student", "medieval", "budget"), (1.0, 2.0)),
    ("Brest", "France", "FR", "France", "BES", (48.39, -4.49), "Europe/Paris", ("coast", "port", "nature", "seaside"), (2.0, 3.0)),
    ("Perpignan", "France", "FR", "France", "PGF", (42.70, 2.90), "Europe/Paris", ("coast", "history", "budget", "seaside"), (2.0, 4.0)),
    ("Clermont-Ferrand", "France", "FR", "France", "CFE", (45.78, 3.09), "Europe/Paris", ("outdoors", "nature", "history", "budget"), (1.0, 3.0)),
    ("Genoa", "Italy", "IT", "Italy", "GOA", (44.41, 8.93), "Europe/Rome", ("port", "history", "oldtown", "food"), (2.0, 3.0)),
    ("Trieste", "Italy", "IT", "Italy", "TRS", (45.65, 13.78), "Europe/Rome", ("port", "coffee", "history", "seaside"), (1.0, 3.0)),
    ("Olbia", "Italy", "IT", "Italy", "OLB", (40.92, 9.50), "Europe/Rome", ("island", "beach", "coast", "romance"), (4.0, 7.0)),
    ("Alghero", "Italy", "IT", "Italy", "AHO", (40.63, 8.29), "Europe/Rome", ("island", "oldtown", "beach", "coast"), (3.0, 6.0)),
    ("Brindisi", "Italy", "IT", "Italy", "BDS", (40.64, 17.94), "Europe/Rome", ("coast", "ancient", "budget", "seaside"), (2.0, 4.0)),
    ("Lamezia Terme", "Italy", "IT", "Italy", "SUF", (38.91, 16.24), "Europe/Rome", ("beach", "coast", "budget", "seaside"), (3.0, 6.0)),
    ("Trapani", "Italy", "IT", "Italy", "TPS", (38.02, 12.51), "Europe/Rome", ("island", "coast", "ancient", "budget"), (3.0, 6.0)),
    ("Dortmund", "Germany", "DE", "Central Europe", "DTM", (51.51, 7.47), "Europe/Berlin", ("modern", "budget", "family"), (1.0, 2.0)),
    ("Munster", "Germany", "DE", "Central Europe", "FMO", (51.96, 7.63), "Europe/Berlin", ("oldtown", "student", "history", "family"), (1.0, 2.0)),
    ("Karlsruhe", "Germany", "DE", "Central Europe", "FKB", (48.99, 8.40), "Europe/Berlin", ("modern", "family", "culture"), (1.0, 2.0)),
    ("Friedrichshafen", "Germany", "DE", "Central Europe", "FDH", (47.66, 9.48), "Europe/Berlin", ("lakes", "nature", "family"), (2.0, 3.0)),
    ("Memmingen", "Germany", "DE", "Central Europe", "FMM", (47.99, 10.18), "Europe/Berlin", ("budget", "medieval", "outdoors"), (1.0, 2.0)),
    ("Erfurt", "Germany", "DE", "Central Europe", "ERF", (50.98, 11.03), "Europe/Berlin", ("medieval", "oldtown", "history", "budget"), (1.0, 2.0)),
    ("Katowice", "Poland", "PL", "Central Europe", "KTW", (50.26, 19.02), "Europe/Warsaw", ("modern", "budget", "culture", "student"), (1.0, 2.0)),
    ("Lodz", "Poland", "PL", "Central Europe", "LCJ", (51.76, 19.46), "Europe/Warsaw", ("art", "hipster", "budget", "modern"), (1.0, 2.0)),
    ("Lublin", "Poland", "PL", "Central Europe", "LUZ", (51.25, 22.57), "Europe/Warsaw", ("oldtown", "history", "student", "budget"), (1.0, 3.0)),
    ("Rzeszow", "Poland", "PL", "Central Europe", "RZE", (50.04, 22.00), "Europe/Warsaw", ("budget", "history", "outdoors"), (1.0, 2.0)),
    ("Szczecin", "Poland", "PL", "Central Europe", "SZZ", (53.43, 14.55), "Europe/Warsaw", ("riverside", "port", "budget", "history"), (1.0, 2.0)),
    ("Ostrava", "Czechia", "CZ", "Central Europe", "OSR", (49.84, 18.29), "Europe/Prague", ("festival", "budget", "industrial", "student"), (1.0, 2.0)),
    ("Kosice", "Slovakia", "SK", "Central Europe", "KSC", (48.72, 21.26), "Europe/Bratislava", ("oldtown", "history", "budget", "cathedral"), (1.0, 2.0)),
    ("Klagenfurt", "Austria", "AT", "Central Europe", "KLU", (46.64, 14.31), "Europe/Vienna", ("lakes", "nature", "outdoors", "family"), (2.0, 4.0)),
    ("Linz", "Austria", "AT", "Central Europe", "LNZ", (48.31, 14.29), "Europe/Vienna", ("modern", "art", "riverside", "culture"), (1.0, 2.0)),
    ("Stavanger", "Norway", "NO", "Nordics", "SVG", (58.97, 5.73), "Europe/Oslo", ("fjords", "nature", "outdoors", "port"), (2.0, 4.0)),
    ("Trondheim", "Norway", "NO", "Nordics", "TRD", (63.43, 10.40), "Europe/Oslo", ("history", "student", "cathedral", "nature"), (2.0, 3.0)),
    ("Alesund", "Norway", "NO", "Nordics", "AES", (62.47, 6.15), "Europe/Oslo", ("fjords", "nature", "design", "outdoors"), (2.0, 4.0)),
    ("Turku", "Finland", "FI", "Nordics", "TKU", (60.45, 22.27), "Europe/Helsinki", ("oldtown", "history", "riverside", "student"), (1.0, 3.0)),
    ("Tampere", "Finland", "FI", "Nordics", "TMP", (61.50, 23.79), "Europe/Helsinki", ("lakes", "modern", "student", "budget"), (2.0, 3.0)),
    ("Oulu", "Finland", "FI", "Nordics", "OUL", (65.01, 25.47), "Europe/Helsinki", ("winter", "student", "outdoors", "budget"), (2.0, 3.0)),
    ("Kalmar", "Sweden", "SE", "Nordics", "KLR", (56.66, 16.36), "Europe/Stockholm", ("medieval", "seaside", "history", "family"), (1.0, 3.0)),
    ("Ohrid", "North Macedonia", "MK", "Balkans", "OHD", (41.12, 20.80), "Europe/Skopje", ("lakes", "oldtown", "ancient", "nature"), (2.0, 4.0)),
    ("Tivat", "Montenegro", "ME", "Balkans", "TIV", (42.40, 18.72), "Europe/Podgorica", ("coast", "beach", "romance", "seaside"), (3.0, 6.0)),
    ("Nis", "Serbia", "RS", "Balkans", "INI", (43.32, 21.90), "Europe/Belgrade", ("history", "ancient", "budget", "student"), (1.0, 2.0)),
    ("Banja Luka", "Bosnia and Herzegovina", "BA", "Balkans", "BNX", (44.77, 17.19), "Europe/Sarajevo", ("nature", "riverside", "budget", "outdoors"), (1.0, 2.0)),
    ("Varna", "Bulgaria", "BG", "Balkans", "VAR", (43.20, 27.91), "Europe/Sofia", ("beach", "coast", "party", "budget"), (3.0, 6.0)),
    ("Burgas", "Bulgaria", "BG", "Balkans", "BOJ", (42.50, 27.47), "Europe/Sofia", ("beach", "seaside", "budget", "family"), (3.0, 7.0)),
    ("Timisoara", "Romania", "RO", "Balkans", "TSR", (45.75, 21.23), "Europe/Bucharest", ("baroque", "art", "student", "budget"), (2.0, 3.0)),
    ("Iasi", "Romania", "RO", "Balkans", "IAS", (47.16, 27.59), "Europe/Bucharest", ("history", "student", "budget", "culture"), (1.0, 3.0)),
    ("Constanta", "Romania", "RO", "Balkans", "CND", (44.17, 28.65), "Europe/Bucharest", ("beach", "ancient", "port", "budget"), (2.0, 5.0)),
    ("Kavala", "Greece", "GR", "Greece", "KVA", (40.94, 24.41), "Europe/Athens", ("coast", "oldtown", "budget", "seaside"), (2.0, 4.0)),
    ("Kalamata", "Greece", "GR", "Greece", "KLX", (37.04, 22.11), "Europe/Athens", ("beach", "coast", "ancient", "food"), (3.0, 6.0)),
    ("Zakynthos", "Greece", "GR", "Greece", "ZTH", (37.75, 20.88), "Europe/Athens", ("island", "beach", "party", "nature"), (3.0, 7.0)),
    ("Kefalonia", "Greece", "GR", "Greece", "EFL", (38.12, 20.50), "Europe/Athens", ("island", "beach", "nature", "romance"), (4.0, 7.0)),
    ("Mytilene", "Greece", "GR", "Greece", "MJT", (39.06, 26.60), "Europe/Athens", ("island", "wine", "nature", "oldtown"), (3.0, 6.0)),
    ("Preveza", "Greece", "GR", "Greece", "PVK", (38.93, 20.77), "Europe/Athens", ("beach", "coast", "ancient", "seaside"), (3.0, 6.0)),
    ("Palanga", "Lithuania", "LT", "Baltics", "PLQ", (55.97, 21.09), "Europe/Vilnius", ("beach", "seaside", "budget", "nature"), (2.0, 4.0)),
    ("Aberdeen", "United Kingdom", "GB", "UK & Ireland", "ABZ", (57.15, -2.09), "Europe/London", ("coast", "history", "port", "nature"), (2.0, 3.0)),
    ("Leeds", "United Kingdom", "GB", "UK & Ireland", "LBA", (53.80, -1.55), "Europe/London", ("shopping", "nightlife", "modern", "budget"), (1.0, 3.0)),
    ("Cardiff", "United Kingdom", "GB", "UK & Ireland", "CWL", (51.48, -3.18), "Europe/London", ("history", "sport", "port", "budget"), (2.0, 3.0)),
    ("Inverness", "United Kingdom", "GB", "UK & Ireland", "INV", (57.48, -4.22), "Europe/London", ("nature", "outdoors", "history", "lakes"), (2.0, 4.0)),
    ("Southampton", "United Kingdom", "GB", "UK & Ireland", "SOU", (50.90, -1.40), "Europe/London", ("port", "coast", "shopping", "budget"), (1.0, 2.0)),
    ("Shannon", "Ireland", "IE", "UK & Ireland", "SNN", (52.70, -8.87), "Europe/Dublin", ("nature", "outdoors", "coast", "history"), (2.0, 4.0)),
    ("Knock", "Ireland", "IE", "UK & Ireland", "NOC", (53.91, -8.82), "Europe/Dublin", ("nature", "outdoors", "budget"), (2.0, 3.0)),
)


def _build() -> tuple[Destination, ...]:
    out: list[Destination] = []
    for (city, country, cc, sub, airport, (lat, lon), tz, tags, (mn, mx)) in _CATALOG:
        prof = profile_from_tags(tags)
        out.append(Destination(
            id=city, name=city, country=country, country_code=cc,
            region="Europe", subregion=sub, timezone=tz,
            latitude=lat, longitude=lon, primary_airport=airport,
            tags=tags, metadata_source="curated",
            recommended_min_days=mn, recommended_max_days=mx,
            enabled=True, acquisition_eligible=True,
            **prof,
        ))
    return tuple(out)


EUROPEAN_CATALOG: tuple[Destination, ...] = _build()

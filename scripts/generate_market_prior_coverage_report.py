"""V9 Search Intelligence Slice 1.5 — generate the Market Prior Coverage
Audit artifact.

A thin CLI wrapper over
:func:`detoura.services.market_prior_coverage_audit.compute_coverage_audit` -
all the actual counting logic lives there (reused directly, not
reimplemented here) so this script and any future caller (an Ops endpoint,
a test) see identical numbers.

Reads the real, currently-configured database (``get_db()`` - the same
``DETOURA_DB_PATH``-driven accessor every other part of this codebase uses,
see ``persistence/db.py``) and writes a deterministic JSON artifact, except
for ``generated_at`` itself.

Run::

    python3 scripts/generate_market_prior_coverage_report.py
    python3 scripts/generate_market_prior_coverage_report.py --out path/to/file.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from detoura.persistence import get_db  # noqa: E402
from detoura.services.market_prior_coverage_audit import compute_coverage_audit  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "generated" / "v9_market_prior_coverage.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    db = get_db()
    audit = compute_coverage_audit(db)
    payload = audit.as_dict()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")

    print(f"Wrote {args.out}")
    print(
        f"coverage: {payload['usable_prior_pairs']}/{payload['possible_pairs']} pairs "
        f"({payload['coverage_pct']}%), {payload['provenance']['total_rows']} rows in denominator"
    )


if __name__ == "__main__":
    main()

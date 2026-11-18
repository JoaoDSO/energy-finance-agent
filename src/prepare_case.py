"""Download and index a separately configured case; no model is launched."""
from __future__ import annotations

import argparse
import re

import workflow


def case_root(case_id: str):
    if not re.fullmatch(r"[a-z0-9_]+", case_id):
        raise ValueError("Case ID must contain only lowercase letters, digits and underscores")
    root = workflow.ROOT / "cases" / case_id
    if not (root / "config/sources.json").is_file():
        raise ValueError("Unknown case configuration")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    args = parser.parse_args()
    try:
        root = case_root(args.case_id)
        workflow.fetch_sources(root)
        workflow.index_sources(root)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Stopped: {error}\n")
    print(f"Prepared local source index for {args.case_id}; no agent or approval started")


if __name__ == "__main__":
    main()

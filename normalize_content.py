"""Normalize selected evidence candidates without mutating source evidence.

Alternative candidates remain available in evidence_candidates; only candidates
marked selected are normalized. Reprocessing is idempotent per normalization
version and historical normalization rows are retained.
"""
from evidence_processing import DB, normalize_selected_candidates


if __name__ == "__main__":
    print(normalize_selected_candidates(DB))

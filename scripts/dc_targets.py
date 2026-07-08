#!/usr/bin/env python3
"""Shared data-center target list for state permit scrapers.

Loads known data center facilities from the lbl-data-center-map combined
dataset (override path with env var DC_COMBINED_CSV) and merges in a curated
list of operator/developer aliases. State scrapers call ``entity_terms(state)``
to get the company-name search terms to run against their agency's permit
database, and ``county_terms(state)`` for county-scoped portals.

Search terms are deliberately short prefixes (agency databases index legal
entity names like "VADATA INC" or "MICROSOFT CORPORATION"), deduplicated and
filtered against terms too generic to search safely.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List

import pandas as pd
from loguru import logger

DEFAULT_COMBINED_CSV = (
    Path.home() / "lbl-data-center-map" / "data" / "processed" / "combined_data_centers.csv"
)

# Operators/developers searched in every state regardless of the facility
# list — brand names differ from the LLC names on permits, so both belong.
NATIONAL_ALIASES = [
    "Amazon", "VADATA", "Amazon Data Services", "Microsoft", "Google",
    "Design LLC",  # Google's permit entity in The Dalles, OR
    "Meta Platforms", "Facebook", "Apple Inc", "Oracle", "Equinix",
    "Digital Realty", "CyrusOne", "QTS", "Quality Technology",
    "Vantage Data", "Aligned Data", "Compass Datacenters", "EdgeConneX",
    "CloudHQ", "DataBank", "Flexential", "Novva", "Sabey", "T5 Data",
    "Prime Data Centers", "Cologix", "CoreSite", "Iron Mountain",
    "NTT", "Stack Infrastructure", "Switch", "Skybox", "Stream Data",
    "Crusoe", "CoreWeave", "Lambda Labs", "xAI", "OpenAI", "Tract Capital",
    "Rowan Digital", "PowerHouse Data", "Corscale", "Yondr", "Ada Infrastructure",
    "STACK Infra", "DataVault", "Core Scientific", "Applied Digital",
    "Cipher Mining", "Riot Platforms", "Marathon Digital",
    "TeraWulf", "Hut 8", "Galaxy Digital", "IREN", "Bitdeer", "Sailion",
]

# Too generic to search a permit database with (flood of false hits).
STOPWORDS = {
    "data", "center", "centers", "datacenter", "campus", "the", "inc", "llc",
    "corp", "corporation", "company", "co", "lp", "ltd", "digital", "cloud",
    "us", "usa", "energy", "switch", "lambda", "tract", "mara", "stream",
    # short brand names that prefix-match unrelated entities in agency DBs
    "meta", "apple", "iren", "vantage", "aligned", "compass", "novva",
}


def _clean_terms(values) -> List[str]:
    out = []
    for v in values:
        if not isinstance(v, str):
            continue
        v = re.sub(r"[^A-Za-z0-9 &-]", " ", v)
        v = re.sub(r"\s+", " ", v).strip()
        if len(v) < 4 or v.lower() in STOPWORDS:
            continue
        out.append(v)
    return out


def load_combined(csv_path: Path | None = None) -> pd.DataFrame:
    path = Path(os.environ.get("DC_COMBINED_CSV", csv_path or DEFAULT_COMBINED_CSV))
    if not path.exists():
        logger.warning(f"Combined DC list not found at {path}; using national aliases only")
        return pd.DataFrame(columns=["state", "company", "operator", "facility_name", "city"])
    return pd.read_csv(path, low_memory=False)


def entity_terms(state: str, csv_path: Path | None = None, include_facilities: bool = False) -> List[str]:
    """Company/operator search terms for a state, most-specific last."""
    df = load_combined(csv_path)
    sub = df[df["state"].astype(str).str.upper() == state.upper()]
    terms = list(NATIONAL_ALIASES)
    terms += _clean_terms(sub["company"].dropna().unique())
    terms += _clean_terms(sub["operator"].dropna().unique())
    if include_facilities:
        terms += _clean_terms(sub["facility_name"].dropna().unique())
    seen, unique = set(), []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    logger.info(f"{state}: {len(unique)} entity search terms ({len(sub)} known facilities)")
    return unique


def county_terms(state: str, csv_path: Path | None = None) -> List[str]:
    df = load_combined(csv_path)
    sub = df[df["state"].astype(str).str.upper() == state.upper()]
    if "county" in sub.columns:
        return sorted(sub["county"].dropna().unique().tolist())
    return []

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


# ==========================================
# SENTINEL - KNF PUBLIC WARNINGS ADAPTER
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "sentinel.db"
OUTPUT = BASE_DIR / "outputs"

SOURCE_KEY = "knf_public_warnings"

SOURCE_URL = (
    "https://www.knf.gov.pl/"
    "en/CONSUMERS/Information_for_the_financial_market_consumers/"
    "Public_warnings"
)

PARSER_VERSION = "knf_warnings_html_v1"

HEADERS = {
    "User-Agent": "SentinelResearchPrototype/0.1",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,pl;q=0.9"
}

FIELDS = [
    "entity_name",
    "identification_numbers",
    "competent_prosecutor_office",
    "judgments_or_proceedings",
    "relevant_information"
]


# ==========================================
# HELPERS
# ==========================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):

    value = unicodedata.normalize("NFKC", value or "")

    value = value.casefold()

    value = re.sub(r"\s+", " ", value).strip()

    return value


def sha256(value):

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def merge_text(old, new):

    new = (new or "").strip()

    if not new:
        return old

    if not old:
        return new

    return old + "\n" + new


# ==========================================
# FETCH ONE PAGE
# ==========================================

def fetch_page():

    print("\nFetching the official KNF page...")

    response = requests.get(
        SOURCE_URL,
        headers=HEADERS,
        timeout=(10, 30)
    )

    response.raise_for_status()

    response.encoding = (
        response.apparent_encoding or "utf-8"
    )

    if len(response.text) < 5000:
        raise RuntimeError(
            "Unexpectedly short HTML response. "
            "Possible access block or incomplete page."
        )

    return response.text


# ==========================================
# SECTION HEADING
# ==========================================

def get_section_heading(table):

    heading = table.find_previous(
        ["h2", "h3", "h4", "h5", "h6"]
    )

    if heading:

        text = heading.get_text(" ", strip=True)

        if text and text.lower() != "public warnings":
            return text

    return "Unclassified KNF warning section"


# ==========================================
# PARSE WARNING TABLES
# ==========================================

def parse_warning_tables(html):

    soup = BeautifulSoup(html, "html.parser")

    page_text = soup.get_text(" ", strip=True)

    count_match = re.search(
        r"Number of warnings found\s*:?\s*([\d,]+)",
        page_text,
        flags=re.IGNORECASE
    )

    displayed_count = (
        int(count_match.group(1).replace(",", ""))
        if count_match else None
    )

    records = []

    tables = soup.select("table.warning-list-table")

    print(f"Warning tables detected: {len(tables)}")

    for table_index, table in enumerate(tables):

        section = get_section_heading(table)

        for tr in table.find_all("tr"):

            cells = tr.find_all(["th", "td"])

            if len(cells) != 6:
                continue

            values = [
                cell.get_text(" ", strip=True)
                for cell in cells
            ]

            # Convert "1 ." into "1"
            number_text = (
                values[0]
                .strip()
                .replace(".", "")
                .strip()
            )

            if not number_text.isdigit():
                continue

            records.append({
                "source_section": section,
                "table_index": table_index,
                "row_number": int(number_text),
                "entity_name": values[1],
                "identification_numbers": values[2],
                "competent_prosecutor_office": values[3],
                "judgments_or_proceedings": values[4],
                "relevant_information": values[5]
            })

    if not records:
        raise RuntimeError(
            f"No warning records parsed. "
            f"Detected {len(tables)} warning tables."
        )

    print(f"Parsed records: {len(records)}")

    return displayed_count, records

# ==========================================
# EXTRACT EXPLICIT DOMAINS
# ==========================================

DOMAIN_PATTERN = re.compile(
    r"(?i)\b(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?:/[^\s<>\"'()]*)?"
)


def extract_domains(text):

    found = set()

    for match in DOMAIN_PATTERN.findall(text or ""):

        candidate = match.rstrip(".,;:!?")

        parsed = urlparse(
            candidate
            if "://" in candidate
            else "https://" + candidate
        )

        domain = parsed.hostname

        if not domain:
            continue

        domain = domain.lower().removeprefix("www.")

        # Exclude the source's own domain
        if domain == "knf.gov.pl":
            continue

        found.add(domain)

    return sorted(found)


# ==========================================
# CHECK DB AND SOURCE
# ==========================================

def open_database():

    if not DB.exists():
        raise FileNotFoundError(f"Database missing: {DB}")

    connection = sqlite3.connect(DB)

    connection.execute("PRAGMA foreign_keys = ON")

    connection.row_factory = sqlite3.Row

    return connection


# ==========================================
# CREATE DISCOVERY CANDIDATES
# ==========================================

def insert_candidate(
    cursor,
    run_id,
    source_id,
    candidate_type,
    candidate_value,
    normalized_value,
    display_name,
    evidence_excerpt,
    trigger_type,
    trigger_reference,
    payload
):

    previous = cursor.execute("""
        SELECT 1
        FROM discovery_candidates
        WHERE candidate_type = ?
        AND normalized_value = ?
        LIMIT 1
    """, (
        candidate_type,
        normalized_value
    )).fetchone()

    status = (
        "duplicate"
        if previous
        else "pending_review"
    )

    cursor.execute("""
        INSERT INTO discovery_candidates (

            run_id,
            source_id,
            candidate_type,
            candidate_value,
            normalized_value,
            display_name,
            discovered_url,
            confidence,
            evidence_excerpt,
            evidence_artifact_id,
            related_pattern_id,
            trigger_type,
            trigger_reference,
            candidate_payload_json,
            status

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, candidate_type, normalized_value) DO NOTHING

    """, (
        run_id,
        source_id,
        candidate_type,
        candidate_value,
        normalized_value,
        display_name,
        SOURCE_URL,
        None,
        evidence_excerpt,
        None,
        None,
        trigger_type,
        trigger_reference,
        json.dumps(payload, ensure_ascii=False),
        status
    ))

    return status


# ==========================================
# MAIN COLLECTION PROCESS
# ==========================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--once",
        action="store_true",
        help="Explicitly perform one manual KNF page retrieval."
    )

    args = parser.parse_args()

    if not args.once:

        print(
            "No collection performed.\n"
            "Run explicitly with: "
            "python3 collect_knf_warnings.py --once"
        )

        return

    OUTPUT.mkdir(exist_ok=True)

    connection = open_database()

    cursor = connection.cursor()

    source = cursor.execute("""
        SELECT id, access_status, enabled, terms_reviewed
        FROM collection_sources
        WHERE source_key = ?
    """, (SOURCE_KEY,)).fetchone()

    if not source:

        connection.close()

        raise RuntimeError(
            "KNF source not found in Source Registry."
        )

    if source["access_status"] not in (
        "manual_only",
        "approved"
    ):

        connection.close()

        raise RuntimeError(
            f"Source status prohibits collection: "
            f"{source['access_status']}"
        )

    source_id = source["id"]

    started = utc_now()

    cursor.execute("""
        INSERT INTO discovery_runs (
            source_id,
            run_type,
            query_json,
            status,
            adapter_name,
            adapter_version,
            started_at,
            initiated_by
        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

    """, (
        source_id,
        "regulatory_snapshot",
        json.dumps({
            "source_url": SOURCE_URL,
            "mode": "single_manual_fetch",
            "parser_version": PARSER_VERSION
        }),
        "running",
        "knf_public_warnings",
        PARSER_VERSION,
        started,
        "manual"
    ))

    run_id = cursor.lastrowid

    connection.commit()

    try:

        html = fetch_page()

        displayed_count, records = parse_warning_tables(html)

        print("\n===== SOURCE INTEGRITY CHECK =====")

        print(f"KNF displayed count: {displayed_count}")
        print(f"Parsed warning records: {len(records)}")

        # Never import incomplete or suspicious snapshots
        if (
            displayed_count is not None
            and displayed_count != len(records)
        ):

            raise RuntimeError(
                "Parsed record count differs from the "
                "count displayed by KNF. No candidates imported. "
                "Inspect table structure before proceeding."
            )

        # Preserve the source snapshot
        retrieved_at = utc_now()

        snapshot = {
            "source": "KNF Public Warnings",
            "source_url": SOURCE_URL,
            "retrieved_at": retrieved_at,
            "displayed_count": displayed_count,
            "parsed_count": len(records),
            "parser_version": PARSER_VERSION,
            "records": records
        }

        snapshot_path = (
            OUTPUT / "knf_public_warnings_snapshot.json"
        )

        snapshot_path.write_text(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        warning_count = 0
        domain_count = 0

        duplicate_count = 0

        for record in records:

            payload = {
                **record,
                "source_url": SOURCE_URL,
                "retrieved_at": retrieved_at
            }

            evidence = "\n".join([
                record["entity_name"],
                record["identification_numbers"],
                record["competent_prosecutor_office"],
                record["judgments_or_proceedings"],
                record["relevant_information"]
            ])

            normalized_record = sha256(
                normalize(
                    record["source_section"]
                    + "|"
                    + evidence
                )
            )

            status = insert_candidate(
                cursor=cursor,
                run_id=run_id,
                source_id=source_id,
                candidate_type="regulatory_warning_record",
                candidate_value=record["entity_name"],
                normalized_value=normalized_record,
                display_name=record["entity_name"][:500],
                evidence_excerpt=evidence[:4000],
                trigger_type="knf_public_warning",
                trigger_reference=(
                    f"{record['source_section']} "
                    f"| row {record['row_number']}"
                ),
                payload=payload
            )

            warning_count += 1

            if status == "duplicate":
                duplicate_count += 1

            # Discover domains explicitly present in the warning
            for domain in extract_domains(
                record["entity_name"] + " "
                + record["relevant_information"]
            ):

                domain_status = insert_candidate(
                    cursor=cursor,
                    run_id=run_id,
                    source_id=source_id,
                    candidate_type="domain",
                    candidate_value=domain,
                    normalized_value=domain,
                    display_name=domain,
                    evidence_excerpt=evidence[:2000],
                    trigger_type="knf_warning_domain",
                    trigger_reference=normalized_record,
                    payload={
                        "domain": domain,
                        "parent_warning_hash": normalized_record,
                        "source_section": record["source_section"],
                        "source_url": SOURCE_URL,
                        "retrieved_at": retrieved_at
                    }
                )

                domain_count += 1

                if domain_status == "duplicate":
                    duplicate_count += 1

        total_candidates = warning_count + domain_count

        cursor.execute("""
            UPDATE discovery_runs

            SET status = 'succeeded',
                items_seen = ?,
                candidates_created = ?,
                finished_at = ?

            WHERE id = ?

        """, (
            warning_count,
            total_candidates,
            utc_now(),
            run_id
        ))

        connection.commit()

        print("\n===== SENTINEL KNF COLLECTION =====")

        print(f"Discovery run ID: {run_id}")
        print(f"Warning records: {warning_count}")
        print(f"Domain candidates: {domain_count}")
        print(f"Total candidates: {total_candidates}")
        print(f"Previously seen candidates: {duplicate_count}")
        print(f"New candidates: {total_candidates - duplicate_count}")

        print("\nSnapshot saved:")
        print(snapshot_path)

        print("\nAll candidates remain subject to review.")
        print("No advertisements were classified as fraudulent.")

    except Exception as error:

        connection.rollback()

        cursor.execute("""
            UPDATE discovery_runs

            SET status = 'failed',
                error_message = ?,
                finished_at = ?

            WHERE id = ?

        """, (
            str(error)[:2000],
            utc_now(),
            run_id
        ))

        connection.commit()

        print("\nCOLLECTION FAILED")
        print(str(error))
        print("No partial candidates were committed.")

        raise

    finally:

        connection.close()


if __name__ == "__main__":
    main()

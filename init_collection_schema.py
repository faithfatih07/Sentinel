import sqlite3
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "sentinel.db"
BACKUP_DIR = BASE_DIR / "backups"

BACKUP_DIR.mkdir(exist_ok=True)

if not DB.exists():
    raise FileNotFoundError("sentinel.db not found")

# ==========================================
# 1. BACKUP BEFORE MIGRATION
# ==========================================

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_path = BACKUP_DIR / f"sentinel_before_collection_schema_{timestamp}.db"

source_db = sqlite3.connect(DB)
backup_db = sqlite3.connect(backup_path)

source_db.backup(backup_db)

backup_db.close()
source_db.close()

print("Database backup created:", backup_path)

# ==========================================
# 2. DATABASE CONNECTION
# ==========================================

connection = sqlite3.connect(DB)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()

# ==========================================
# 3. SOURCE REGISTRY
# ==========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS collection_sources (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    source_key TEXT NOT NULL UNIQUE,

    source_name TEXT NOT NULL,

    source_type TEXT NOT NULL,

    platform TEXT,

    base_url TEXT,

    jurisdiction TEXT,

    geographic_scope TEXT,

    languages_json TEXT,

    content_types_json TEXT,

    access_method TEXT,

    access_status TEXT NOT NULL DEFAULT 'not_assessed'
        CHECK(access_status IN (
            'not_assessed',
            'approved',
            'restricted',
            'manual_only',
            'disabled'
        )),

    terms_url TEXT,

    terms_reviewed INTEGER NOT NULL DEFAULT 0
        CHECK(terms_reviewed IN (0, 1)),

    auth_required INTEGER NOT NULL DEFAULT 0
        CHECK(auth_required IN (0, 1)),

    rate_limit_notes TEXT,

    permitted_use_notes TEXT,

    enabled INTEGER NOT NULL DEFAULT 0
        CHECK(enabled IN (0, 1)),

    last_checked_at TIMESTAMP,

    notes TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# ==========================================
# 4. DISCOVERY TARGETS
# ==========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS discovery_targets (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    target_type TEXT NOT NULL,

    target_value TEXT NOT NULL,

    normalized_value TEXT NOT NULL,

    display_name TEXT,

    origin_source_id INTEGER,

    parent_target_id INTEGER,

    trigger_pattern_id INTEGER,

    discovery_method TEXT NOT NULL,

    confidence REAL
        CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1),

    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK(status IN (
            'proposed',
            'pending_review',
            'approved',
            'active',
            'rejected',
            'exhausted'
        )),

    rationale TEXT,

    evidence_json TEXT,

    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    last_checked_at TIMESTAMP,

    next_check_at TIMESTAMP,

    reviewed_at TIMESTAMP,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(target_type, normalized_value),

    FOREIGN KEY(origin_source_id)
        REFERENCES collection_sources(id),

    FOREIGN KEY(parent_target_id)
        REFERENCES discovery_targets(id),

    FOREIGN KEY(trigger_pattern_id)
        REFERENCES patterns(id)
)
""")

# ==========================================
# 5. DISCOVERY RUNS
# ==========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS discovery_runs (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    source_id INTEGER NOT NULL,

    target_id INTEGER,

    run_type TEXT NOT NULL,

    query_json TEXT,

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN (
            'queued',
            'running',
            'succeeded',
            'partial',
            'failed',
            'cancelled'
        )),

    adapter_name TEXT,

    adapter_version TEXT,

    items_seen INTEGER DEFAULT 0,

    candidates_created INTEGER DEFAULT 0,

    started_at TIMESTAMP,

    finished_at TIMESTAMP,

    error_message TEXT,

    initiated_by TEXT DEFAULT 'system',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(source_id)
        REFERENCES collection_sources(id),

    FOREIGN KEY(target_id)
        REFERENCES discovery_targets(id)
)
""")

# ==========================================
# 6. DISCOVERY CANDIDATES
# ==========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS discovery_candidates (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    run_id INTEGER NOT NULL,

    source_id INTEGER NOT NULL,

    candidate_type TEXT NOT NULL,

    candidate_value TEXT NOT NULL,

    normalized_value TEXT NOT NULL,

    display_name TEXT,

    discovered_url TEXT,

    confidence REAL
        CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1),

    evidence_excerpt TEXT,

    evidence_artifact_id INTEGER,

    related_pattern_id INTEGER,

    trigger_type TEXT,

    trigger_reference TEXT,

    candidate_payload_json TEXT,

    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK(status IN (
            'new',
            'pending_review',
            'approved',
            'rejected',
            'duplicate',
            'promoted'
        )),

    review_notes TEXT,

    reviewed_at TIMESTAMP,

    promoted_target_id INTEGER,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(run_id, candidate_type, normalized_value),

    FOREIGN KEY(run_id)
        REFERENCES discovery_runs(id),

    FOREIGN KEY(source_id)
        REFERENCES collection_sources(id),

    FOREIGN KEY(evidence_artifact_id)
        REFERENCES extracted_artifacts(id),

    FOREIGN KEY(related_pattern_id)
        REFERENCES patterns(id),

    FOREIGN KEY(promoted_target_id)
        REFERENCES discovery_targets(id)
)
""")

# ==========================================
# 7. INDEXES
# ==========================================

indexes = [

    """
    CREATE INDEX IF NOT EXISTS idx_sources_enabled
    ON collection_sources(enabled, access_status)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_targets_status
    ON discovery_targets(status, target_type)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_targets_parent
    ON discovery_targets(parent_target_id)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_runs_source
    ON discovery_runs(source_id, status)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_runs_target
    ON discovery_runs(target_id)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_candidates_status
    ON discovery_candidates(status)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_candidates_pattern
    ON discovery_candidates(related_pattern_id)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_candidates_value
    ON discovery_candidates(candidate_type, normalized_value)
    """
]

for statement in indexes:
    cursor.execute(statement)

# ==========================================
# 8. SCHEMA VERSION
# ==========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS schema_versions (

    version INTEGER PRIMARY KEY,

    description TEXT NOT NULL,

    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
INSERT OR IGNORE INTO schema_versions (
    version,
    description
)
VALUES (
    2,
    'Initial collection and discovery architecture'
)
""")

connection.commit()

# ==========================================
# 9. VERIFY SCHEMA
# ==========================================

required_tables = [
    "collection_sources",
    "discovery_targets",
    "discovery_runs",
    "discovery_candidates",
    "schema_versions"
]

print("\n===== SENTINEL COLLECTION SCHEMA =====")

for table in required_tables:

    exists = cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
    """, (table,)).fetchone()

    if not exists:
        raise RuntimeError(f"Missing table: {table}")

    count = cursor.execute(
        f'SELECT COUNT(*) FROM "{table}"'
    ).fetchone()[0]

    print(f"{table}: {count} records | READY")

# Foreign key integrity
foreign_key_errors = cursor.execute(
    "PRAGMA foreign_key_check"
).fetchall()

print("\nForeign key errors:", len(foreign_key_errors))

if foreign_key_errors:
    print(foreign_key_errors)
    raise RuntimeError("Foreign key integrity check failed.")

connection.close()

print("\nCollection schema initialized successfully.")
print("Existing advertisement and evidence records were preserved.")
print("All collection sources remain disabled until reviewed.")
print("Database backup:", backup_path)

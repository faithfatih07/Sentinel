import sqlite3
import shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
DB = BASE / "sentinel.db"
BACKUPS = BASE / "backups"

BACKUPS.mkdir(exist_ok=True)

# 1. Backup
backup = BACKUPS / (
    f"sentinel_before_collection_"
    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
)

shutil.copy2(DB, backup)

connection = sqlite3.connect(DB)
cursor = connection.cursor()

# 2. Add provenance field if missing
columns = {
    row[1]
    for row in cursor.execute(
        "PRAGMA table_info(advertisements)"
    ).fetchall()
}

if "record_provenance" not in columns:
    cursor.execute("""
        ALTER TABLE advertisements
        ADD COLUMN record_provenance TEXT NOT NULL
        DEFAULT 'unknown'
    """)

# 3. Classify only the known sample records
cursor.execute("""
    UPDATE advertisements
    SET record_provenance = 'synthetic_demo'
    WHERE company = 'BrokerX'
    AND url LIKE 'https://example.com/%'
""")

cursor.execute("""
    UPDATE advertisements
    SET record_provenance = 'local_test_sample'
    WHERE url LIKE 'local-test://%'
""")

connection.commit()

# 4. Report
rows = cursor.execute("""
    SELECT id, company, platform, url, record_provenance
    FROM advertisements
    ORDER BY id
""").fetchall()

print("\n===== DATA PROVENANCE REPORT =====")

for row in rows:
    print("\nID:", row[0])
    print("Company:", row[1])
    print("Platform:", row[2])
    print("URL:", row[3])
    print("Provenance:", row[4])

print("\nBackup:", backup)
print("\nNo advertisement records were deleted.")

connection.close()

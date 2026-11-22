
import sqlite3
import hashlib
import json
import re
import subprocess

from pathlib import Path

# ----------------------------------------
# PATHS
# ----------------------------------------

ROOT = Path(__file__).resolve().parent / "sentinel_video_test"

VIDEO = ROOT / "source.mp4"
AUDIO = ROOT / "audio" / "source.wav"
SRT = ROOT / "audio" / "transcript.srt"

DB = Path(__file__).with_name("sentinel.db")


# ----------------------------------------
# FILE VALIDATION
# ----------------------------------------

for path in [VIDEO, AUDIO, SRT]:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")


# ----------------------------------------
# FILE FINGERPRINT
# ----------------------------------------

def file_hash(path):

    sha = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)

    return sha.hexdigest()


# ----------------------------------------
# MEDIA METADATA
# ----------------------------------------

def get_metadata(path):

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path)
        ],
        capture_output=True,
        text=True,
        check=True
    )

    data = json.loads(result.stdout)

    video_stream = next(
        (s for s in data["streams"] if s["codec_type"] == "video"),
        {}
    )

    duration = data.get("format", {}).get("duration")

    return {
        "duration": float(duration) if duration else None,
        "width": video_stream.get("width"),
        "height": video_stream.get("height")
    }


# ----------------------------------------
# SRT TIMESTAMP CONVERSION
# ----------------------------------------

def to_seconds(timestamp):

    timestamp = timestamp.replace(",", ".")

    hours, minutes, seconds = timestamp.split(":")

    return (
        int(hours) * 3600
        + int(minutes) * 60
        + float(seconds)
    )


# ----------------------------------------
# PARSE WHISPER SRT
# ----------------------------------------

def parse_srt(path):

    raw = path.read_text(encoding="utf-8-sig")

    blocks = re.split(r"\r?\n\s*\r?\n", raw.strip())

    segments = []

    for block in blocks:

        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip()
        ]

        timestamp_index = next(
            (
                i for i, line in enumerate(lines)
                if "-->" in line
            ),
            None
        )

        if timestamp_index is None:
            continue

        start_text, end_text = lines[timestamp_index].split(
            "-->", 1
        )

        start = to_seconds(start_text.strip())

        end = to_seconds(end_text.strip().split()[0])

        text = " ".join(lines[timestamp_index + 1:]).strip()

        if text:
            segments.append({
                "start": start,
                "end": end,
                "text": text
            })

    return segments


# ----------------------------------------
# DATABASE CONNECTION
# ----------------------------------------

connection = sqlite3.connect(DB)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()


# Ensure media tables exist

cursor.execute("""
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    advertisement_id INTEGER NOT NULL,
    parent_asset_id INTEGER,
    asset_type TEXT NOT NULL,
    original_url TEXT,
    local_path TEXT,
    mime_type TEXT,
    sha256_hash TEXT,
    duration_seconds REAL,
    width INTEGER,
    height INTEGER,
    file_size_bytes INTEGER,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(advertisement_id)
        REFERENCES advertisements(id),
    FOREIGN KEY(parent_asset_id)
        REFERENCES media_assets(id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS extracted_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    advertisement_id INTEGER NOT NULL,
    media_asset_id INTEGER,
    artifact_type TEXT NOT NULL,
    text_content TEXT,
    start_seconds REAL,
    end_seconds REAL,
    extraction_method TEXT,
    model_name TEXT,
    confidence REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(advertisement_id)
        REFERENCES advertisements(id),
    FOREIGN KEY(media_asset_id)
        REFERENCES media_assets(id)
)
""")

connection.commit()


# ----------------------------------------
# CREATE A LOCAL TEST RECORD
# Not classified as a real advertisement
# ----------------------------------------

test_url = "local-test://sentinel/video-sample-001"

cursor.execute(
    "SELECT id FROM advertisements WHERE url = ?",
    (test_url,)
)

row = cursor.fetchone()

if row:
    advertisement_id = row[0]

else:
    cursor.execute("""
        INSERT INTO advertisements
        (company, platform, advertiser, content, url)
        VALUES (?, ?, ?, ?, ?)
    """, (
        None,
        "local_test",
        None,
        "Unclassified local video-processing test",
        test_url
    ))

    advertisement_id = cursor.lastrowid


# ----------------------------------------
# REGISTER VIDEO AND AUDIO ASSETS
# ----------------------------------------

def register_asset(path, asset_type, mime_type, parent_id=None):

    digest = file_hash(path)

    cursor.execute("""
        SELECT id FROM media_assets
        WHERE advertisement_id = ?
        AND sha256_hash = ?
    """, (advertisement_id, digest))

    existing = cursor.fetchone()

    if existing:
        return existing[0]

    metadata = get_metadata(path)

    cursor.execute("""
        INSERT INTO media_assets (
            advertisement_id,
            parent_asset_id,
            asset_type,
            local_path,
            mime_type,
            sha256_hash,
            duration_seconds,
            width,
            height,
            file_size_bytes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        advertisement_id,
        parent_id,
        asset_type,
        str(path),
        mime_type,
        digest,
        metadata["duration"],
        metadata["width"],
        metadata["height"],
        path.stat().st_size
    ))

    return cursor.lastrowid


video_asset_id = register_asset(
    VIDEO,
    "video",
    "video/mp4"
)

audio_asset_id = register_asset(
    AUDIO,
    "audio",
    "audio/wav",
    parent_id=video_asset_id
)


# ----------------------------------------
# IMPORT WHISPER TRANSCRIPTION
# ----------------------------------------

segments = parse_srt(SRT)

saved = 0
skipped = 0

for segment in segments:

    cursor.execute("""
        SELECT id
        FROM extracted_artifacts
        WHERE media_asset_id = ?
        AND artifact_type = 'transcript'
        AND start_seconds = ?
        AND end_seconds = ?
        AND text_content = ?
    """, (
        audio_asset_id,
        segment["start"],
        segment["end"],
        segment["text"]
    ))

    if cursor.fetchone():
        skipped += 1
        continue

    cursor.execute("""
        INSERT INTO extracted_artifacts (
            advertisement_id,
            media_asset_id,
            artifact_type,
            text_content,
            start_seconds,
            end_seconds,
            extraction_method,
            model_name,
            confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        advertisement_id,
        audio_asset_id,
        "transcript",
        segment["text"],
        segment["start"],
        segment["end"],
        "whisper.cpp",
        "ggml-small",
        None
    ))

    saved += 1


connection.commit()


# ----------------------------------------
# RESULTS
# ----------------------------------------

print("\n--- SENTINEL WHISPER INGESTION ---")

print(f"Advertisement record: {advertisement_id}")
print(f"Video asset ID: {video_asset_id}")
print(f"Audio asset ID: {audio_asset_id}")

print(f"\nWhisper segments found: {len(segments)}")
print(f"New segments saved: {saved}")
print(f"Existing segments skipped: {skipped}")

print("\n--- STORED TRANSCRIPT SAMPLE ---")

cursor.execute("""
    SELECT start_seconds, end_seconds, text_content
    FROM extracted_artifacts
    WHERE advertisement_id = ?
    AND artifact_type = 'transcript'
    ORDER BY start_seconds
    LIMIT 5
""", (advertisement_id,))

for start, end, text in cursor.fetchall():
    print(f"[{start:.2f}s - {end:.2f}s] {text}")

print("\nWhisper data successfully imported into Sentinel!")

connection.close()


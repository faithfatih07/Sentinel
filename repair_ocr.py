
import sqlite3
import csv
import json
import hashlib
import subprocess
import tempfile

from pathlib import Path

ROOT = Path(__file__).with_name("sentinel_video_test")
FRAMES_DIR = ROOT / "frames"
DB = Path(__file__).with_name("sentinel.db")

LANGUAGE = "tur+eng"
PSM = "6"
MIN_CONFIDENCE = 20

MODEL_NAME = "tesseract_tsv_v2_tur_eng_psm6"


def file_hash(path):
    digest = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


# ----------------------------------------
# Generate an actual TSV file
# ----------------------------------------

def run_ocr(image_path):

    with tempfile.TemporaryDirectory() as temp_dir:

        output_base = Path(temp_dir) / "ocr_result"

        subprocess.run(
            [
                "tesseract",
                str(image_path),
                str(output_base),
                "-l", LANGUAGE,
                "--psm", PSM,
                "tsv"
            ],
            capture_output=True,
            text=True,
            check=True
        )

        tsv_path = Path(str(output_base) + ".tsv")

        if not tsv_path.exists():
            raise RuntimeError("Tesseract did not create a TSV file.")

        with open(tsv_path, "r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, delimiter="\t")

            required = {
                "level", "block_num", "par_num",
                "line_num", "left", "top",
                "width", "height", "conf", "text"
            }

            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(
                    f"Unexpected TSV columns: {reader.fieldnames}"
                )

            lines = {}
            word_boxes = []
            confidences = []

            for row in reader:

                if row["level"] != "5":
                    continue

                word = " ".join((row.get("text") or "").replace("\t", " ").split())

                if not word:
                    continue

                confidence = float(row["conf"])

                bbox = {
                    "x": int(row["left"]),
                    "y": int(row["top"]),
                    "width": int(row["width"]),
                    "height": int(row["height"])
                }

                word_boxes.append({
                    "text": word,
                    "confidence": confidence,
                    "bbox": bbox
                })

                if confidence < MIN_CONFIDENCE:
                    continue

                line_key = (
                    row["block_num"],
                    row["par_num"],
                    row["line_num"]
                )

                lines.setdefault(line_key, []).append(word)
                confidences.append(confidence)

        extracted_text = "\n".join(
            " ".join(words)
            for words in lines.values()
        ).strip()

        extracted_text = extracted_text.replace("\t", " ").strip()

        average_confidence = (
            sum(confidences) / len(confidences)
            if confidences else None
        )

        return {
            "text": extracted_text,
            "confidence": average_confidence,
            "word_boxes": word_boxes
        }


# ----------------------------------------
# Database
# ----------------------------------------

connection = sqlite3.connect(DB)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()

columns = {
    row[1]
    for row in cursor.execute(
        "PRAGMA table_info(extracted_artifacts)"
    ).fetchall()
}

if "extraction_region" not in columns:
    cursor.execute("""
        ALTER TABLE extracted_artifacts
        ADD COLUMN extraction_region TEXT
    """)

if "metadata_json" not in columns:
    cursor.execute("""
        ALTER TABLE extracted_artifacts
        ADD COLUMN metadata_json TEXT
    """)

cursor.execute("""
    SELECT id
    FROM advertisements
    WHERE url = ?
""", ("local-test://sentinel/video-sample-001",))

ad = cursor.fetchone()

if not ad:
    raise RuntimeError("Test advertisement not found.")

advertisement_id = ad[0]

cursor.execute("""
    SELECT id, width, height
    FROM media_assets
    WHERE advertisement_id = ?
    AND asset_type = 'video'
    ORDER BY id
    LIMIT 1
""", (advertisement_id,))

video = cursor.fetchone()

if not video:
    raise RuntimeError("Original video asset not found.")

video_id, width, height = video

frames = sorted(FRAMES_DIR.glob("frame_*.jpg"))

saved = 0
empty = 0

print("\n--- SENTINEL OCR REPAIR V2 ---")


# ----------------------------------------
# Process each frame
# ----------------------------------------

for frame in frames:

    try:
        frame_number = int(frame.stem.split("_")[1])
    except (ValueError, IndexError):
        continue

    timestamp = float(frame_number - 1)

    digest = file_hash(frame)

    # Find the image belonging to this exact frame path
    cursor.execute("""
        SELECT id
        FROM media_assets
        WHERE advertisement_id = ?
        AND asset_type = 'image'
        AND local_path = ?
    """, (advertisement_id, str(frame)))

    asset = cursor.fetchone()

    if asset:
        frame_asset_id = asset[0]

    else:
        cursor.execute("""
            INSERT INTO media_assets (
                advertisement_id,
                parent_asset_id,
                asset_type,
                local_path,
                mime_type,
                sha256_hash,
                width,
                height,
                file_size_bytes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            advertisement_id,
            video_id,
            "image",
            str(frame),
            "image/jpeg",
            digest,
            width,
            height,
            frame.stat().st_size
        ))

        frame_asset_id = cursor.lastrowid

    # Run fresh OCR using the temporary TSV file
    result = run_ocr(frame)
    text = result["text"]

    # Locate previous PSM 6 OCR record for this frame
    cursor.execute("""
        SELECT id
        FROM extracted_artifacts
        WHERE media_asset_id = ?
        AND artifact_type = 'ocr_text'
        AND extraction_method = 'tesseract'
        AND start_seconds = ?
        AND model_name LIKE '%psm6%'
        ORDER BY id DESC
        LIMIT 1
    """, (frame_asset_id, timestamp))

    old = cursor.fetchone()

    if not text:

        if old:
            cursor.execute("""
                DELETE FROM extracted_artifacts
                WHERE id = ?
            """, (old[0],))

        empty += 1
        continue

    metadata = {
        "frame_number": frame_number,
        "language": LANGUAGE,
        "psm": PSM,
        "capture_type": "screen_capture",
        "word_boxes": result["word_boxes"]
    }

    values = (
        advertisement_id,
        frame_asset_id,
        "ocr_text",
        text,
        timestamp,
        timestamp,
        "tesseract",
        MODEL_NAME,
        result["confidence"],
        "full_frame",
        json.dumps(metadata, ensure_ascii=False)
    )

    if old:

        cursor.execute("""
            UPDATE extracted_artifacts
            SET advertisement_id = ?,
                media_asset_id = ?,
                artifact_type = ?,
                text_content = ?,
                start_seconds = ?,
                end_seconds = ?,
                extraction_method = ?,
                model_name = ?,
                confidence = ?,
                extraction_region = ?,
                metadata_json = ?
            WHERE id = ?
        """, values + (old[0],))

    else:

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
                confidence,
                extraction_region,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)

    saved += 1
    connection.commit()

    print(
        f"[{timestamp:.0f}s] {frame.name}: "
        f"{len(text)} characters | "
        f"confidence: {result['confidence']}"
    )


# ----------------------------------------
# Final verification
# ----------------------------------------

cursor.execute("""
    SELECT
        COUNT(*),
        SUM(
            CASE
                WHEN instr(text_content, char(9)) > 0
                THEN 1 ELSE 0
            END
        ),
        MAX(length(text_content))
    FROM extracted_artifacts
    WHERE advertisement_id = ?
    AND artifact_type = 'ocr_text'
    AND model_name = ?
""", (advertisement_id, MODEL_NAME))

total, tab_leaks, max_length = cursor.fetchone()

print("\n--- FINAL VERIFICATION ---")
print(f"Corrected records: {saved}")
print(f"Empty frames: {empty}")
print(f"Verified PSM 6 records: {total}")
print(f"Records containing tab leaks: {tab_leaks or 0}")
print(f"Maximum text length: {max_length or 0}")

if tab_leaks:
    print("WARNING: TSV parsing issue still exists.")
else:
    print("TSV parsing verified successfully!")

connection.close()


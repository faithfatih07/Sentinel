import csv
import subprocess
import tempfile

from pathlib import Path

image = Path("sentinel_video_test/frames/frame_015.jpg")

with tempfile.TemporaryDirectory() as temp:
    output = Path(temp) / "test"

    subprocess.run([
        "tesseract",
        str(image),
        str(output),
        "-l", "tur+eng",
        "--psm", "6",
        "tsv"
    ], check=True)

    tsv = Path(str(output) + ".tsv")

    print("\n--- RAW TSV STRUCTURE ---")

    with open(tsv, encoding="utf-8-sig", newline="") as f:

        reader = csv.reader(f, delimiter="\t")

        for i, row in enumerate(reader):
            print(f"\nRow {i} | Columns: {len(row)}")

            for index, value in enumerate(row):
                print(f"  Column {index}: {repr(value)}")

            if i >= 3:
                break

    print("\n--- DICTREADER TEST ---")

    with open(tsv, encoding="utf-8-sig", newline="") as f:

        reader = csv.DictReader(f, delimiter="\t")

        print("Headers:", reader.fieldnames)

        for row in reader:
            if row.get("level") == "5":
                print("Extracted text field:", repr(row.get("text")))
                break

print("\nDiagnostic complete.")

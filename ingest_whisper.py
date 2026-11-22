"""Run real whisper.cpp inference for the isolated Sentinel sample video."""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

from evidence_processing import begin_stage, finish_stage, process_database

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "sentinel_video_test"
VIDEO = DATA / "source.mp4"
AUDIO = DATA / "audio" / "source.wav"
MODEL = ROOT / "sentinel_models" / "ggml-small.bin"
DB = ROOT / "sentinel.db"
WHISPER = shutil.which("whisper-cli")
LANGUAGE = "auto"
THREADS = 4


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def media_metadata(path):
    result = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)
    ], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0) or None,
        "width": video_stream.get("width"), "height": video_stream.get("height"),
    }


def to_seconds(value):
    h, m, s = value.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(path):
    text = path.read_text(encoding="utf-8-sig")
    segments = []
    for block in __import__("re").split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        ix = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ix is None:
            continue
        left, right = lines[ix].split("-->", 1)
        body = "\n".join(lines[ix + 1:])
        if body.strip():
            segments.append({"start": to_seconds(left), "end": to_seconds(right.split()[0]), "text": body})
    return segments


def json_segments(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    segs = data.get("transcription", data.get("segments", []))
    result = []
    for segment in segs:
        offsets = segment.get("offsets") or {}
        start = segment.get("start")
        end = segment.get("end")
        if start is None and offsets.get("from") is not None:
            start = float(offsets["from"]) / 1000
        if end is None and offsets.get("to") is not None:
            end = float(offsets["to"]) / 1000
        if isinstance(start, str):
            start = to_seconds(start)
        if isinstance(end, str):
            end = to_seconds(end)
        tokens = segment.get("tokens") or []
        probs = [float(t["p"]) for t in tokens if isinstance(t, dict) and isinstance(t.get("p"), (float, int))]
        result.append({"start": start, "end": end, "text": segment.get("text", "").strip(),
                       "confidence": sum(probs) / len(probs) if probs else None})
    return data, result


def main():
    for path in (VIDEO, AUDIO, MODEL):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not WHISPER:
        raise RuntimeError("whisper-cli not found; real ASR inference cannot run")

    run_id = str(uuid.uuid4())
    version_result = subprocess.run([WHISPER, "--version"], capture_output=True, text=True, check=True)
    software_version = (version_result.stdout or version_result.stderr).strip()
    audio_hash, model_hash = sha256(AUDIO), sha256(MODEL)
    config = {"language": LANGUAGE, "threads": THREADS, "beam_size": 5, "best_of": 5,
              "temperature": 0.0, "output_json_full": True, "output_srt": True}
    config_hash = hashlib.sha256(json.dumps([audio_hash, model_hash, software_version, config], sort_keys=True).encode()).hexdigest()
    outdir = ROOT / "sentinel_video_test" / "audio" / "asr_runs" / config_hash
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = outdir / "whisper"
    srt_path, json_path = prefix.with_suffix(".srt"), prefix.with_suffix(".json")
    stage_id = begin_stage(DB, "whisper_inference_ingestion", {
        "extraction_run_id": run_id, "engine": "whisper.cpp", "model": MODEL.name,
        "software_version": software_version, "configuration_hash": config_hash,
    })
    try:
        inference_performed = not (srt_path.is_file() and json_path.is_file())
        if inference_performed:
            command = [WHISPER, "-m", str(MODEL), "-l", LANGUAGE, "-t", str(THREADS), "-bs", "5", "-bo", "5",
                       "-tp", "0", "-ojf", "-osrt", "-of", str(prefix), str(AUDIO)]
            proc = subprocess.run(command, capture_output=True, text=True, check=True)
            (outdir / "stdout.log").write_text(proc.stdout, encoding="utf-8")
            (outdir / "stderr.log").write_text(proc.stderr, encoding="utf-8")
        srt_segments = parse_srt(srt_path)
        json_data, inferred = json_segments(json_path)
        # SRT carries the definitive intervals and text layout. JSON token scores
        # are attached by segment order only when the count aligns.
        if len(inferred) == len(srt_segments):
            for srt, js in zip(srt_segments, inferred):
                srt["confidence"] = js["confidence"]
        else:
            for srt in srt_segments:
                srt["confidence"] = None

        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA foreign_keys=ON")
        ad = conn.execute("SELECT id FROM advertisements WHERE url=?", ("local-test://sentinel/video-sample-001",)).fetchone()
        if not ad:
            raise RuntimeError("Isolated sample advertisement is missing")
        advertisement_id = ad[0]

        def register(path, asset_type, mime, parent_id=None):
            digest = sha256(path)
            row = conn.execute("SELECT id FROM media_assets WHERE advertisement_id=? AND sha256_hash=?", (advertisement_id, digest)).fetchone()
            if row:
                return row[0]
            meta = media_metadata(path)
            return conn.execute("""INSERT INTO media_assets(advertisement_id,parent_asset_id,asset_type,local_path,mime_type,sha256_hash,duration_seconds,width,height,file_size_bytes)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (advertisement_id,parent_id,asset_type,str(path),mime,digest,meta["duration"],meta["width"],meta["height"],path.stat().st_size)).lastrowid

        video_id = register(VIDEO, "video", "video/mp4")
        audio_id = register(AUDIO, "audio", "audio/wav", video_id)
        conn.commit()
        inserted = skipped = 0
        for segment_index, segment in enumerate(srt_segments):
            existing = conn.execute("SELECT metadata_json FROM extracted_artifacts WHERE media_asset_id=? AND artifact_type='transcript'", (audio_id,)).fetchall()
            duplicate = False
            for (meta_raw,) in existing:
                try:
                    meta = json.loads(meta_raw or "{}")
                except (TypeError, ValueError):
                    continue
                if meta.get("configuration_hash") == config_hash:
                    duplicate = conn.execute("""SELECT 1 FROM extracted_artifacts WHERE media_asset_id=? AND artifact_type='transcript'
                        AND start_seconds=? AND end_seconds=? AND text_content=? AND json_extract(metadata_json,'$.configuration_hash')=? LIMIT 1""",
                        (audio_id,segment["start"],segment["end"],segment["text"],config_hash)).fetchone() is not None
                    if duplicate:
                        break
            if duplicate:
                skipped += 1
                continue
            language = json_data.get("result", {}).get("language") or json_data.get("language")
            confidence = segment.get("confidence")
            meta = {"extraction_run_id": run_id, "engine": "whisper.cpp", "model": MODEL.name,
                    "model_sha256": model_hash, "software_version": software_version,
                    "language": language or "unknown_not_recorded", "language_requested": LANGUAGE,
                    "configuration": config, "configuration_hash": config_hash,
                    "confidence_scale": "token_probability_mean_0_1" if confidence is not None else "missing",
                    "confidence_method": "mean JSON token p values; uncalibrated" if confidence is not None else "not_available",
                    "source_audio_asset_id": audio_id, "source_video_asset_id": video_id,
                    "audio_sha256": audio_hash, "raw_srt_path": str(srt_path.relative_to(ROOT)),
                    "raw_json_path": str(json_path.relative_to(ROOT)), "raw_srt_sha256": sha256(srt_path),
                    "raw_json_sha256": sha256(json_path), "raw_json_segment": inferred[segment_index] if len(inferred)==len(srt_segments) else None}
            conn.execute("""INSERT INTO extracted_artifacts(advertisement_id,media_asset_id,artifact_type,text_content,start_seconds,end_seconds,extraction_method,model_name,confidence,metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (advertisement_id,audio_id,"transcript",segment["text"],segment["start"],segment["end"],"whisper.cpp",MODEL.name,confidence,json.dumps(meta,ensure_ascii=False,sort_keys=True)))
            inserted += 1
        conn.commit()
        finish_stage(DB, stage_id, {"inference_performed":inference_performed,"segments":len(srt_segments),"inserted":inserted,"skipped":skipped,
                                    "source_audio_asset_id":audio_id,"source_video_asset_id":video_id,"configuration_hash":config_hash,
                                    "raw_srt":str(srt_path.relative_to(ROOT)),"raw_json":str(json_path.relative_to(ROOT))})
        conn.close()
        evidence = process_database(DB)
        print(json.dumps({"engine":"whisper.cpp","software_version":software_version,"inference_performed":inference_performed,
                          "run_id":run_id,"segments":len(srt_segments),"inserted":inserted,"skipped":skipped,
                          "srt":str(srt_path.relative_to(ROOT)),"json":str(json_path.relative_to(ROOT)),"evidence":evidence},indent=2))
    except Exception as error:
        try:
            finish_stage(DB,stage_id,error=repr(error))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()

"""Persistent, deterministic evidence catalog and selection layer.

Association convention: transcript intervals are half-open [start, end). A frame
at t is associated with every valid transcript interval where start <= t < end.
No timestamp is inferred from a filename.
"""
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

DB = Path(__file__).with_name("sentinel.db")
TEST_URL = "local-test://sentinel/video-sample-001"
SCHEMA_VERSION = 10
SCORER_VERSION = "ocr-asr-policy-v3"
NORMALIZATION_VERSION = "nfkc_tr_v2"


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("İ", "i").replace("I", "ı").casefold()
    text = re.sub(r"[^\w\s%€$£₺.,:+/-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_confidence(value, modality, scale=None):
    """Return (raw, explicit scale, [0,1] value); unknown remains NULL."""
    if value is None:
        return None, scale or "missing", None
    raw = float(value)
    if scale in ("percent", "0-100") or (scale is None and modality == "visual"):
        return raw, "percent_0_100", max(0.0, min(1.0, raw / 100.0))
    if scale in ("unit_interval", "0-1", "unit_interval_0_1", "token_probability_mean_0_1") or (scale is None and modality == "audio" and 0 <= raw <= 1):
        return raw, scale if scale == "token_probability_mean_0_1" else "unit_interval_0_1", max(0.0, min(1.0, raw))
    if modality == "audio" and 1 < raw <= 100:
        return raw, "percent_0_100", raw / 100.0
    return raw, scale or "unknown", None


def quality_assessment_ocr(text):
    """Heuristic OCR readability score; not a calibrated accuracy probability."""
    s = (text or "").strip()
    if not s:
        return "empty", None, {"reason": "no readable text"}
    words = re.findall(r"[^\W_]+", s, flags=re.UNICODE)
    nums = re.findall(r"(?<!\w)-?\d+(?:\.\d+)?(?!\w)", s)
    has_header = all(k in s.lower() for k in ("level", "left", "top", "conf", "text"))
    numeric_dump = len(nums) >= 25 and len(nums) / max(1, len(s.split())) > .25
    if has_header or numeric_dump:
        return "contaminated", 0.0, {"reason": "TSV header or numeric TSV-like dump", "numeric_tokens": len(nums)}
    if not words:
        return "low_quality", 0.0, {"reason": "no lexical tokens"}
    alnum = sum(c.isalnum() for c in s)
    letters = sum(c.isalpha() for c in s)
    letter_ratio = letters / max(1, alnum)
    tokens = [w.casefold() for w in words]
    repeats = sum(a == b for a, b in zip(tokens, tokens[1:])) / max(1, len(tokens)-1)
    one_char = sum(len(w) == 1 and not w.isdigit() for w in words) / len(words)
    completeness=min(1.0, len(words)/12)
    score=max(0.0,min(100.0,45*letter_ratio+25*(1-repeats)-20*one_char+10*completeness))
    status="low_quality" if len(words)<2 or score<25 or one_char>.75 else "candidate"
    return status,round(score,2),{"policy":"ocr_readability_v1","letter_ratio":round(letter_ratio,4),"adjacent_repeat_ratio":round(repeats,4),"single_letter_ratio":round(one_char,4),"word_count":len(words),"completeness_proxy":round(completeness,4)}


def quality_assessment_asr(text, start, end, confidence_scale):
    """ASR segment plausibility checks, separate from OCR policy and confidence."""
    s=(text or "").strip(); words=re.findall(r"[^\W_]+",s,flags=re.UNICODE)
    if not s: return "empty",None,{"policy":"asr_segment_v1","reason":"empty transcript"}
    duration=(end-start) if start is not None and end is not None else None
    if duration is None or start<0 or end<=start:
        return "invalid_timestamp",0.0,{"policy":"asr_segment_v1","reason":"missing or invalid interval"}
    wps=len(words)/duration
    if wps>8 or wps<0.15:
        return "low_quality",round(max(0,min(100,100-abs(wps-3)*10)),2),{"policy":"asr_segment_v1","words_per_second":round(wps,3),"reason":"implausible segment completeness/density"}
    return "candidate",round(max(0,min(100,100-abs(wps-3)*10)),2),{"policy":"asr_segment_v1","words_per_second":round(wps,3),"confidence_scale":confidence_scale,"language_consistency":"unknown_not_evaluated"}


def quality_assessment(text, modality):
    # Backwards-compatible helper for callers/tests; modality policies stay separate.
    if modality=="visual": return quality_assessment_ocr(text)
    return quality_assessment_asr(text,0,1,"unknown")


def candidate_key(source_id, frame_asset_id, modality, engine, model, software, language, config):
    stable = json.dumps([source_id, frame_asset_id, modality, engine, model, software, language, config], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode()).hexdigest()


def apply_migrations(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS evidence_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        current = conn.execute("SELECT COALESCE(MAX(version),0) FROM evidence_schema_migrations").fetchone()[0]
        if current < 1:
            existing={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "evidence_normalizations" in existing and "evidence_normalizations_legacy" not in existing:
                conn.execute("ALTER TABLE evidence_normalizations RENAME TO evidence_normalizations_legacy")
            statements = [
              """CREATE TABLE evidence_candidates (
                candidate_id TEXT PRIMARY KEY, candidate_key TEXT NOT NULL UNIQUE,
                source_artifact_id INTEGER, advertisement_id INTEGER NOT NULL,
                media_asset_id INTEGER, modality TEXT NOT NULL CHECK(modality IN ('visual','audio')),
                frame_asset_id INTEGER, frame_filename TEXT, frame_timestamp_seconds REAL,
                interval_start_seconds REAL, interval_end_seconds REAL,
                engine TEXT NOT NULL, model TEXT, software_version TEXT, language TEXT,
                configuration_json TEXT NOT NULL, confidence_raw REAL, confidence_scale TEXT NOT NULL,
                confidence_normalized REAL, raw_output TEXT NOT NULL, plain_text TEXT NOT NULL,
                structured_output_json TEXT, contamination_payload TEXT,
                quality_status TEXT NOT NULL, quality_score REAL, quality_components_json TEXT NOT NULL,
                selection_status TEXT NOT NULL DEFAULT 'unprocessed', selection_reason TEXT NOT NULL DEFAULT '',
                scorer_version TEXT NOT NULL, candidate_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(source_artifact_id) REFERENCES extracted_artifacts(id),
                FOREIGN KEY(advertisement_id) REFERENCES advertisements(id),
                FOREIGN KEY(media_asset_id) REFERENCES media_assets(id),
                FOREIGN KEY(frame_asset_id) REFERENCES media_assets(id))""",
              "CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence_candidates(source_artifact_id)",
              "CREATE INDEX IF NOT EXISTS idx_evidence_frame ON evidence_candidates(frame_asset_id,modality)",
              """CREATE TABLE evidence_selection_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT, scorer_version TEXT NOT NULL,
                tie_break_policy TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
              """CREATE TABLE evidence_associations (
                frame_asset_id INTEGER NOT NULL, transcript_candidate_id TEXT NOT NULL,
                frame_timestamp_seconds REAL NOT NULL, audio_start_seconds REAL NOT NULL,
                audio_end_seconds REAL NOT NULL, association_type TEXT NOT NULL DEFAULT 'timestamp_overlap',
                PRIMARY KEY(frame_asset_id,transcript_candidate_id),
                FOREIGN KEY(frame_asset_id) REFERENCES media_assets(id),
                FOREIGN KEY(transcript_candidate_id) REFERENCES evidence_candidates(candidate_id))""",
              """CREATE TRIGGER IF NOT EXISTS evidence_candidate_raw_immutable BEFORE UPDATE OF candidate_key,source_artifact_id,advertisement_id,media_asset_id,modality,frame_asset_id,frame_filename,frame_timestamp_seconds,interval_start_seconds,interval_end_seconds,engine,model,software_version,language,configuration_json,confidence_raw,confidence_scale,confidence_normalized,raw_output,plain_text,structured_output_json,contamination_payload ON evidence_candidates BEGIN SELECT RAISE(ABORT,'raw evidence is immutable'); END""",
              "CREATE TRIGGER IF NOT EXISTS evidence_candidate_no_delete BEFORE DELETE ON evidence_candidates BEGIN SELECT RAISE(ABORT,'evidence candidates cannot be deleted'); END",
            ]
            for sql in statements: conn.execute(sql)
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (1)")
            current = 1
        if current < 2:
            # The legacy source schema does not record the OCR/ASR binary version.
            # Persist that absence explicitly rather than leaving provenance ambiguous.
            conn.execute("DROP TRIGGER IF EXISTS evidence_candidate_raw_immutable")
            conn.execute("UPDATE evidence_candidates SET software_version='unknown_not_recorded' WHERE software_version IS NULL")
            conn.execute("""CREATE TRIGGER evidence_candidate_raw_immutable
              BEFORE UPDATE OF candidate_key,source_artifact_id,advertisement_id,media_asset_id,modality,frame_asset_id,frame_filename,
              frame_timestamp_seconds,interval_start_seconds,interval_end_seconds,engine,model,software_version,language,configuration_json,
              confidence_raw,confidence_scale,confidence_normalized,raw_output,plain_text,structured_output_json,contamination_payload
              ON evidence_candidates BEGIN SELECT RAISE(ABORT,'raw evidence is immutable'); END""")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (2)")
            current = 2
        if current < 3:
            conn.execute("""CREATE TABLE IF NOT EXISTS evidence_stage_runs (
              stage_run_id INTEGER PRIMARY KEY AUTOINCREMENT, stage_name TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
              error_text TEXT, metadata_json TEXT NOT NULL DEFAULT '{}')""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_stage_status ON evidence_stage_runs(stage_name,status,started_at)")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (3)")
            current=3
        if current < 4:
            cols={r[1] for r in conn.execute("PRAGMA table_info(evidence_candidates)")}
            for name,decl in (("extraction_run_id","TEXT"),("source_video_asset_id","INTEGER"),("media_validation_status","TEXT NOT NULL DEFAULT 'unknown'"),("timestamp_validation_status","TEXT NOT NULL DEFAULT 'unknown'"),("last_selection_run_id","INTEGER")):
                if name not in cols: conn.execute(f"ALTER TABLE evidence_candidates ADD COLUMN {name} {decl}")
            existing={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "evidence_normalizations" in existing and "evidence_normalizations_legacy" not in existing:
                conn.execute("ALTER TABLE evidence_normalizations RENAME TO evidence_normalizations_legacy")
            conn.execute("""CREATE TABLE IF NOT EXISTS evidence_normalizations (
              candidate_id TEXT NOT NULL, normalization_version TEXT NOT NULL,
              source_artifact_id INTEGER, normalized_text TEXT NOT NULL, normalized_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(candidate_id,normalization_version),
              FOREIGN KEY(candidate_id) REFERENCES evidence_candidates(candidate_id),
              FOREIGN KEY(source_artifact_id) REFERENCES extracted_artifacts(id))""")
            conn.execute("""CREATE TABLE evidence_selection_decisions (
              run_id INTEGER NOT NULL,candidate_id TEXT NOT NULL,selection_status TEXT NOT NULL,
              quality_score REAL,selection_reason TEXT NOT NULL,
              PRIMARY KEY(run_id,candidate_id),
              FOREIGN KEY(run_id) REFERENCES evidence_selection_runs(run_id),
              FOREIGN KEY(candidate_id) REFERENCES evidence_candidates(candidate_id))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_norm_source ON evidence_normalizations(source_artifact_id)")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (4)")
            current=4
        if current < 5:
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "evidence_stage_runs" in tables and "evidence_stage_runs_legacy" not in tables:
                conn.execute("ALTER TABLE evidence_stage_runs RENAME TO evidence_stage_runs_legacy")
            conn.execute("""CREATE TABLE IF NOT EXISTS evidence_stage_runs (
              stage_run_id INTEGER PRIMARY KEY AUTOINCREMENT, stage_name TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','interrupted','retryable')),
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
              error_text TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
              attempt_number INTEGER NOT NULL DEFAULT 1, retry_of_stage_run_id INTEGER,
              FOREIGN KEY(retry_of_stage_run_id) REFERENCES evidence_stage_runs(stage_run_id))""")
            if "evidence_stage_runs_legacy" in tables:
                conn.execute("""INSERT OR IGNORE INTO evidence_stage_runs
                  (stage_run_id,stage_name,status,started_at,finished_at,error_text,metadata_json)
                  SELECT stage_run_id,stage_name,
                    CASE status WHEN 'succeeded' THEN 'completed' WHEN 'running' THEN 'interrupted' ELSE status END,
                    started_at,finished_at,error_text,metadata_json FROM evidence_stage_runs_legacy""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_stage_retry ON evidence_stage_runs(stage_name,status,attempt_number)")
            conn.execute("DROP VIEW IF EXISTS current_selected_matching_input")
            conn.execute("""CREATE VIEW current_selected_matching_input AS
              SELECT n.id,n.advertisement_id,c.modality,c.plain_text AS original_text,
                n.start_seconds,n.end_seconds,c.confidence_raw AS confidence,c.model AS source_model,
                en.normalized_text,en.normalized_hash,en.normalization_version,c.candidate_id,c.source_artifact_id
              FROM evidence_candidates c
              JOIN evidence_normalizations en ON en.candidate_id=c.candidate_id
              JOIN normalized_artifacts n ON n.source_artifact_id=c.source_artifact_id
              WHERE c.selection_status='selected' AND c.quality_status='candidate'
                AND c.media_validation_status='valid' AND c.timestamp_validation_status='valid'
                AND en.normalization_version='nfkc_tr_v2'""")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (5)")
            current=5
        if current < 6:
            # Confidence normalization is derived from immutable raw confidence.
            # Recognize whisper.cpp token-probability means as an explicit 0-1 scale.
            conn.execute("DROP TRIGGER IF EXISTS evidence_candidate_raw_immutable")
            for candidate_id, raw, scale in conn.execute("SELECT candidate_id,confidence_raw,confidence_scale FROM evidence_candidates WHERE confidence_scale='token_probability_mean_0_1' AND confidence_raw IS NOT NULL"):
                normalized=max(0.0,min(1.0,float(raw))) if 0 <= float(raw) <= 1 else None
                conn.execute("UPDATE evidence_candidates SET confidence_normalized=? WHERE candidate_id=?",(normalized,candidate_id))
            conn.execute("""CREATE TRIGGER evidence_candidate_raw_immutable
              BEFORE UPDATE OF candidate_key,source_artifact_id,advertisement_id,media_asset_id,modality,frame_asset_id,frame_filename,
              frame_timestamp_seconds,interval_start_seconds,interval_end_seconds,engine,model,software_version,language,configuration_json,
              confidence_raw,confidence_scale,raw_output,plain_text,structured_output_json,contamination_payload
              ON evidence_candidates BEGIN SELECT RAISE(ABORT,'raw evidence is immutable'); END""")
            # Keep the legacy artifact id for pattern_matches foreign keys, while
            # exposing text only through the current selected candidate and its
            # current versioned normalization. Legacy normalized rows remain intact.
            conn.execute("DROP VIEW IF EXISTS current_selected_matching_input")
            conn.execute("""CREATE VIEW current_selected_matching_input AS
              SELECT n.id,n.advertisement_id,c.modality,c.plain_text AS original_text,
                n.start_seconds,n.end_seconds,c.confidence_raw AS confidence,c.model AS source_model,
                en.normalized_text,en.normalized_hash,en.normalization_version,c.candidate_id,c.source_artifact_id
              FROM evidence_candidates c
              JOIN evidence_normalizations en ON en.candidate_id=c.candidate_id
              JOIN normalized_artifacts n ON n.source_artifact_id=c.source_artifact_id
              WHERE c.selection_status='selected' AND c.quality_status='candidate'
                AND c.media_validation_status='valid' AND c.timestamp_validation_status='valid'
                AND en.normalization_version='nfkc_tr_v2'""")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (6)")
            current=6
        if current < 7:
            # v5 archived historical stage rows before copying them. Import them
            # into the new state machine with fresh IDs while retaining the
            # untouched legacy table and recording each legacy ID in metadata.
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "evidence_stage_runs_legacy" in tables:
                legacy_rows=conn.execute("SELECT stage_run_id,stage_name,status,started_at,finished_at,error_text,metadata_json FROM evidence_stage_runs_legacy ORDER BY stage_run_id").fetchall()
                for legacy_id,name,status,started,finished,error,raw_metadata in legacy_rows:
                    old_status={"succeeded":"completed","running":"interrupted"}.get(status,status)
                    if old_status not in ("pending","running","completed","failed","interrupted","retryable"):
                        old_status="failed"
                    try:
                        details=json.loads(raw_metadata or "{}")
                    except (TypeError,ValueError):
                        details={"legacy_metadata_raw":raw_metadata}
                    wrapped=json.dumps({"migrated_legacy_stage_run_id":legacy_id,"legacy_metadata":details},sort_keys=True)
                    conn.execute("""INSERT INTO evidence_stage_runs(stage_name,status,started_at,finished_at,error_text,metadata_json,attempt_number)
                      VALUES (?,?,?,?,?,?,1)""",(name,old_status,started,finished,error,wrapped))
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (7)")
            current=7
        if current < 8:
            conn.execute("""CREATE TABLE IF NOT EXISTS pattern_discovery_runs (
              run_id TEXT PRIMARY KEY,
              status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
              discovery_version TEXT NOT NULL,
              input_count INTEGER NOT NULL DEFAULT 0,
              config_json TEXT NOT NULL DEFAULT '{}',
              error_text TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              completed_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pattern_discovery_candidates (
              candidate_id TEXT PRIMARY KEY,
              fingerprint TEXT NOT NULL UNIQUE,
              proposed_text TEXT NOT NULL,
              normalized_text TEXT NOT NULL,
              language TEXT NOT NULL,
              candidate_type TEXT NOT NULL CHECK(candidate_type IN ('company_brand','service_description','recurring_phrase','business_terminology','other')),
              description TEXT,
              confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
              confidence_basis TEXT NOT NULL,
              discovery_status TEXT NOT NULL CHECK(discovery_status IN ('pending_review','possible_duplicate','duplicate','deferred','approved','rejected')),
              duplicate_pattern_id INTEGER,
              duplicate_candidate_id TEXT,
              suggested_category_key TEXT,
              approved_pattern_id INTEGER,
              proposed_by TEXT NOT NULL CHECK(proposed_by IN ('local_discovery','ai_assisted')),
              proposal_json TEXT NOT NULL DEFAULT '{}',
              first_run_id TEXT NOT NULL,
              last_seen_run_id TEXT NOT NULL,
              reviewer TEXT,
              review_note TEXT,
              reviewed_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(duplicate_pattern_id) REFERENCES patterns(id),
              FOREIGN KEY(duplicate_candidate_id) REFERENCES pattern_discovery_candidates(candidate_id),
              FOREIGN KEY(approved_pattern_id) REFERENCES patterns(id),
              FOREIGN KEY(first_run_id) REFERENCES pattern_discovery_runs(run_id),
              FOREIGN KEY(last_seen_run_id) REFERENCES pattern_discovery_runs(run_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pattern_discovery_candidate_runs (
              run_id TEXT NOT NULL,
              candidate_id TEXT NOT NULL,
              confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
              rationale TEXT NOT NULL,
              PRIMARY KEY(run_id,candidate_id),
              FOREIGN KEY(run_id) REFERENCES pattern_discovery_runs(run_id),
              FOREIGN KEY(candidate_id) REFERENCES pattern_discovery_candidates(candidate_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pattern_discovery_candidate_evidence (
              candidate_id TEXT NOT NULL,
              source_candidate_id TEXT NOT NULL,
              source_artifact_id INTEGER NOT NULL,
              evidence_excerpt TEXT NOT NULL,
              excerpt_hash TEXT NOT NULL,
              first_run_id TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(candidate_id,source_candidate_id,excerpt_hash),
              FOREIGN KEY(candidate_id) REFERENCES pattern_discovery_candidates(candidate_id),
              FOREIGN KEY(source_candidate_id) REFERENCES evidence_candidates(candidate_id),
              FOREIGN KEY(source_artifact_id) REFERENCES extracted_artifacts(id),
              FOREIGN KEY(first_run_id) REFERENCES pattern_discovery_runs(run_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pattern_discovery_reviews (
              review_id INTEGER PRIMARY KEY AUTOINCREMENT,
              candidate_id TEXT NOT NULL,
              decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','deferred')),
              reviewer TEXT NOT NULL,
              note TEXT NOT NULL,
              approved_pattern_id INTEGER,
              reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(candidate_id) REFERENCES pattern_discovery_candidates(candidate_id),
              FOREIGN KEY(approved_pattern_id) REFERENCES patterns(id))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_candidates_status ON pattern_discovery_candidates(discovery_status,language,candidate_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_evidence_source ON pattern_discovery_candidate_evidence(source_artifact_id,source_candidate_id)")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS discovery_review_no_update BEFORE UPDATE ON pattern_discovery_reviews
              BEGIN SELECT RAISE(ABORT,'pattern discovery reviews are append-only'); END""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS discovery_review_no_delete BEFORE DELETE ON pattern_discovery_reviews
              BEGIN SELECT RAISE(ABORT,'pattern discovery reviews are append-only'); END""")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (8)")
            current=8
        if current < 9:
            cols={r[1] for r in conn.execute("PRAGMA table_info(pattern_discovery_candidates)")}
            if "is_current" not in cols:
                conn.execute("ALTER TABLE pattern_discovery_candidates ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_current_status ON pattern_discovery_candidates(is_current,discovery_status)")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (9)")
            current=9
        if current < 10:
            cols={r[1] for r in conn.execute("PRAGMA table_info(pattern_discovery_candidates)")}
            if "novelty_type" not in cols:
                conn.execute("ALTER TABLE pattern_discovery_candidates ADD COLUMN novelty_type TEXT CHECK(novelty_type IN ('new_pattern','variant','possible_duplicate'))")
            if "reasoning" not in cols:
                conn.execute("ALTER TABLE pattern_discovery_candidates ADD COLUMN reasoning TEXT")
            if "target_pattern_id" not in cols:
                conn.execute("ALTER TABLE pattern_discovery_candidates ADD COLUMN target_pattern_id INTEGER REFERENCES patterns(id)")
            conn.execute("INSERT INTO evidence_schema_migrations(version) VALUES (10)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _metadata_json(raw):
    try:
        value=json.loads(raw) if raw else None
        return value if isinstance(value, (dict,list)) else None
    except (ValueError, TypeError):
        return None


def _process_database(db_path=DB):
    conn=sqlite3.connect(db_path)
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    ad=conn.execute("SELECT id FROM advertisements WHERE url=?",(TEST_URL,)).fetchone()
    if not ad:
        conn.close(); raise RuntimeError("Test advertisement not found")
    ad_id=ad["id"]
    rows=conn.execute("""SELECT e.*,m.asset_type,m.local_path,m.advertisement_id AS asset_ad_id,
      m.parent_asset_id,m.id AS joined_asset_id,p.asset_type AS parent_type,p.advertisement_id AS parent_ad_id,p.duration_seconds AS parent_duration
      FROM extracted_artifacts e LEFT JOIN media_assets m ON m.id=e.media_asset_id
      LEFT JOIN media_assets p ON p.id=m.parent_asset_id
      WHERE e.advertisement_id=? AND e.artifact_type IN ('ocr_text','transcript') ORDER BY e.id""",(ad_id,)).fetchall()
    run_id=conn.execute("INSERT INTO evidence_selection_runs(scorer_version,tie_break_policy) VALUES (?,?)",
      (SCORER_VERSION,"quality_score DESC, confidence_normalized DESC NULLS LAST, plain_text_length DESC, candidate_key ASC")).lastrowid
    by_frame={}; candidate_rows=[]
    for r in rows:
        mod="visual" if r["artifact_type"]=="ocr_text" else "audio"
        expected="image" if mod=="visual" else "audio"
        asset_valid=(r["joined_asset_id"] is not None and r["asset_type"]==expected and r["asset_ad_id"]==r["advertisement_id"] and r["parent_type"]=="video" and r["parent_ad_id"]==r["advertisement_id"])
        raw=r["text_content"] or ""
        start,end=r["start_seconds"],r["end_seconds"]
        ts=None; interval_start=interval_end=None
        timestamp_status="valid"
        if mod=="visual":
            if start is not None and start>=0 and (end is None or abs(end-start)<1e-6): ts=float(start)
            else: timestamp_status="missing_or_invalid"
            match=re.fullmatch(r"frame_(\d+)\.jpg",Path(r["local_path"] or "").name)
            if not match: timestamp_status="malformed_filename"
            elif ts is not None and abs((int(match.group(1))-1)-ts)>1e-6: timestamp_status="filename_timestamp_mismatch"
            if ts is not None and r["parent_duration"] is not None and ts>=r["parent_duration"]: timestamp_status="outside_video_duration"
        else:
            if start is not None and end is not None and start>=0 and end>start:
                interval_start=float(start); interval_end=float(end)
                if r["parent_duration"] is not None and end>r["parent_duration"]: timestamp_status="outside_video_duration"
            else: timestamp_status="missing_or_invalid"
        if mod=="visual": qstatus,qscore,qparts=quality_assessment_ocr(raw)
        else: qstatus,qscore,qparts=quality_assessment_asr(raw,start,end,"missing" if r["confidence"] is None else "unknown")
        if timestamp_status!="valid": qstatus="invalid_timestamp" if mod=="visual" else "missing_or_invalid_timestamp"
        if not asset_valid: qstatus="invalid_media_association"
        meta=_metadata_json(r["metadata_json"] if "metadata_json" in r.keys() else None)
        explicit_scale=meta.get("confidence_scale") if isinstance(meta,dict) else None
        conf,scale,conf_norm=normalize_confidence(r["confidence"],mod,explicit_scale)
        struct=None
        if isinstance(meta,dict):
            struct=meta.get("word_boxes") or meta.get("words")
        structured=json.dumps(struct,ensure_ascii=False,sort_keys=True) if struct is not None else None
        extraction_run_id=meta.get("extraction_run_id") if isinstance(meta,dict) else None
        # Retain exact stored extraction output; quarantine it from plain text on contamination.
        contaminated=qstatus=="contaminated"
        plain="" if contaminated else raw
        payload=raw if contaminated else None
        cfg=json.dumps({"model_name":r["model_name"],"language":meta.get("language") if isinstance(meta,dict) else None,
          "page_segmentation_mode":meta.get("page_segmentation_mode") if isinstance(meta,dict) else None,
          "capture_type":meta.get("capture_type") if isinstance(meta,dict) else None},ensure_ascii=False,sort_keys=True)
        lang=(meta.get("language") if isinstance(meta,dict) else None) or ("tur+eng" if "tur" in (r["model_name"] or "").lower() else None)
        engine=r["extraction_method"] or "unknown"; model=r["model_name"]
        ck=candidate_key(r["id"],r["media_asset_id"] if mod=="visual" else None,mod,engine,model,None,lang,cfg)
        cid=ck
        selection="unprocessed" if qstatus=="candidate" else qstatus
        software_version=(meta.get("software_version") if isinstance(meta,dict) else None) or "unknown_not_recorded"
        extraction_run_id=extraction_run_id or "unknown_not_recorded"
        conn.execute("""INSERT OR IGNORE INTO evidence_candidates
          (candidate_id,candidate_key,source_artifact_id,advertisement_id,media_asset_id,modality,frame_asset_id,frame_filename,
           frame_timestamp_seconds,interval_start_seconds,interval_end_seconds,engine,model,software_version,language,configuration_json,
           confidence_raw,confidence_scale,confidence_normalized,raw_output,plain_text,structured_output_json,contamination_payload,
           quality_status,quality_score,quality_components_json,selection_status,selection_reason,scorer_version,extraction_run_id,
           source_video_asset_id,media_validation_status,timestamp_validation_status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (cid,ck,r["id"],ad_id,r["media_asset_id"],mod,r["media_asset_id"] if mod=="visual" else None,
           Path(r["local_path"] or "").name if mod=="visual" else None,ts,interval_start,interval_end,engine,model,software_version,lang,cfg,
           conf,scale,conf_norm,raw,plain,structured,payload,qstatus,qscore,json.dumps(qparts,sort_keys=True),selection,
           "awaiting selection" if selection=="unprocessed" else qstatus,SCORER_VERSION,extraction_run_id,
           r["parent_asset_id"],"valid" if asset_valid else "invalid",timestamp_status))
        conn.execute("UPDATE evidence_candidates SET extraction_run_id=COALESCE(extraction_run_id,?),source_video_asset_id=COALESCE(source_video_asset_id,?),media_validation_status=?,timestamp_validation_status=? WHERE candidate_id=?",
          (extraction_run_id,r["parent_asset_id"],"valid" if asset_valid else "invalid",timestamp_status,cid))
        candidate_rows.append((cid,mod,r["media_asset_id"],ts,interval_start,interval_end,qstatus,qscore,conf_norm,plain))
        if mod=="visual" and r["media_asset_id"] is not None: by_frame.setdefault(r["media_asset_id"],[]).append(candidate_rows[-1])
    # Clear any prior selection that no longer corresponds to a source row
    # evaluated in this run (for example, changed source configuration/type).
    current_ids={item[0] for item in candidate_rows}
    old_selected=conn.execute("SELECT candidate_id,quality_score FROM evidence_candidates WHERE advertisement_id=? AND selection_status='selected'",(ad_id,)).fetchall()
    for old in old_selected:
        if old["candidate_id"] not in current_ids:
            reason="deselected: source candidate absent from current selection run"
            conn.execute("UPDATE evidence_candidates SET selection_status='alternative',selection_reason=?,last_selection_run_id=? WHERE candidate_id=?",(reason,run_id,old["candidate_id"]))
            conn.execute("INSERT OR IGNORE INTO evidence_selection_decisions VALUES (?,?,?,?,?)",(run_id,old["candidate_id"],"alternative",old["quality_score"],reason))
    # Stable frame placeholder identity; no filename-derived timestamp.
    frames=conn.execute("SELECT id,advertisement_id,local_path FROM media_assets WHERE advertisement_id=? AND asset_type='image' ORDER BY id",(ad_id,)).fetchall()
    for f in frames:
        if not by_frame.get(f["id"]):
            ck=hashlib.sha256(f"missing-frame:{f['id']}".encode()).hexdigest()
            conn.execute("""INSERT OR IGNORE INTO evidence_candidates
              (candidate_id,candidate_key,advertisement_id,media_asset_id,modality,frame_asset_id,frame_filename,
               engine,software_version,configuration_json,confidence_scale,raw_output,plain_text,quality_status,quality_components_json,
               selection_status,selection_reason,scorer_version)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ck,ck,ad_id,f["id"],"visual",f["id"],Path(f["local_path"] or "").name,"none","unknown_not_recorded","{}","missing","","","missing","{}","missing","no source artifact for this frame",SCORER_VERSION))
            by_frame[f["id"]]=[]
    # Selection updates are intentionally separate from immutable source/provenance fields.
    for frame_id,items in by_frame.items():
        eligible=[x for x in items if x[6]=="candidate"]
        winner=sorted(eligible,key=lambda x:(-(x[7] if x[7] is not None else -1),-(x[8] if x[8] is not None else -1),-len(x[9]),x[0]))[0] if eligible else None
        for x in items:
            status="selected" if winner and x[0]==winner[0] else "alternative" if x[6]=="candidate" else x[6]
            reason="winner by versioned score, confidence, text length, candidate key" if status=="selected" else "eligible candidate retained" if status=="alternative" else x[6]
            conn.execute("UPDATE evidence_candidates SET selection_status=?,selection_reason=?,last_selection_run_id=? WHERE candidate_id=?",(status,reason,run_id,x[0]))
            conn.execute("INSERT OR IGNORE INTO evidence_selection_decisions VALUES (?,?,?,?,?)",(run_id,x[0],status,x[7],reason))
    for x in candidate_rows:
        if x[1]=="audio":
            st="selected" if x[6]=="candidate" else x[6]
            conn.execute("UPDATE evidence_candidates SET selection_status=?,selection_reason=?,last_selection_run_id=? WHERE candidate_id=?",(st,"ASR policy retains valid segments; no cross-segment winner selection",run_id,x[0]))
            conn.execute("INSERT OR IGNORE INTO evidence_selection_decisions VALUES (?,?,?,?,?)",(run_id,x[0],st,x[7],"ASR segment retained independently"))
    # Rebuild derived associations atomically; only valid image/audio assets and stored valid times.
    conn.execute("DELETE FROM evidence_associations")
    conn.execute("""INSERT INTO evidence_associations(frame_asset_id,transcript_candidate_id,frame_timestamp_seconds,audio_start_seconds,audio_end_seconds)
      SELECT v.frame_asset_id,a.candidate_id,v.frame_timestamp_seconds,a.interval_start_seconds,a.interval_end_seconds
      FROM evidence_candidates v JOIN evidence_candidates a ON a.advertisement_id=v.advertisement_id
      WHERE v.modality='visual' AND v.selection_status='selected' AND a.modality='audio' AND v.frame_timestamp_seconds IS NOT NULL
      AND a.interval_start_seconds IS NOT NULL AND a.interval_end_seconds IS NOT NULL
      AND a.interval_start_seconds<=v.frame_timestamp_seconds AND v.frame_timestamp_seconds<a.interval_end_seconds
      AND v.media_validation_status='valid' AND v.timestamp_validation_status='valid'
      AND a.media_validation_status='valid' AND a.timestamp_validation_status='valid'""")
    conn.commit()
    counts=dict(conn.execute("SELECT selection_status,COUNT(*) FROM evidence_candidates WHERE advertisement_id=? GROUP BY selection_status",(ad_id,)))
    result={"migration_version":SCHEMA_VERSION,"selection_run_id":run_id,"source_artifacts":len(rows),"candidate_rows":conn.execute("SELECT COUNT(*) FROM evidence_candidates WHERE advertisement_id=?",(ad_id,)).fetchone()[0],"frames":len(frames),"counts":counts,"associations":conn.execute("SELECT COUNT(*) FROM evidence_associations e JOIN media_assets m ON m.id=e.frame_asset_id WHERE m.advertisement_id=?",(ad_id,)).fetchone()[0]}
    conn.close(); return result


def normalize_selected_candidates(db_path=DB):
    """Add versioned normalized representations for selected candidates only."""
    run=begin_stage(db_path,"normalization",{"normalization_version":NORMALIZATION_VERSION})
    conn=sqlite3.connect(db_path); conn.row_factory=sqlite3.Row
    try:
        rows=conn.execute("""SELECT c.candidate_id,c.source_artifact_id,c.plain_text,c.advertisement_id,c.modality,
          c.confidence_raw,c.model,a.start_seconds,a.end_seconds
          FROM evidence_candidates c LEFT JOIN extracted_artifacts a ON a.id=c.source_artifact_id
          WHERE c.selection_status='selected' AND c.quality_status='candidate'
            AND c.media_validation_status='valid' AND c.timestamp_validation_status='valid'
          ORDER BY c.candidate_id""").fetchall()
        made=0
        for r in rows:
            normalized=normalize_text(r["plain_text"])
            if not normalized: continue
            cur=conn.execute("INSERT OR IGNORE INTO evidence_normalizations(candidate_id,normalization_version,source_artifact_id,normalized_text,normalized_hash) VALUES (?,?,?,?,?)",
              (r["candidate_id"],NORMALIZATION_VERSION,r["source_artifact_id"],normalized,hashlib.sha256(normalized.encode()).hexdigest()))
            made+=cur.rowcount
            # Backward-compatible additive mirror consumed by the existing matcher.
            # Never update an existing legacy normalized row or its source text.
            conn.execute("""INSERT OR IGNORE INTO normalized_artifacts
              (source_artifact_id,advertisement_id,modality,original_text,normalized_text,normalized_hash,
               start_seconds,end_seconds,confidence,source_model,normalization_version)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (r["source_artifact_id"],r["advertisement_id"],r["modality"],r["plain_text"],normalized,
               hashlib.sha256(normalized.encode()).hexdigest(),r["start_seconds"],r["end_seconds"],
               r["confidence_raw"],r["model"],NORMALIZATION_VERSION))
        conn.commit(); conn.close()
        finish_stage(db_path,run,{"selected_seen":len(rows),"new_normalizations":made,"version":NORMALIZATION_VERSION})
        return {"selected_seen":len(rows),"new_normalizations":made,"run_id":run}
    except Exception as error:
        conn.rollback(); conn.close()
        finish_stage(db_path,run,error=repr(error))
        raise


def begin_stage(db_path, stage_name, metadata=None):
    conn=sqlite3.connect(db_path)
    apply_migrations(conn)
    # Only age out genuinely abandoned runs. A live running stage is never reset
    # just because another caller starts the same stage.
    conn.execute("UPDATE evidence_stage_runs SET status='interrupted',finished_at=CURRENT_TIMESTAMP,error_text=COALESCE(error_text,'process disappeared before stage completion') WHERE stage_name=? AND status='running' AND started_at < datetime('now','-30 minutes')",(stage_name,))
    conn.execute("UPDATE evidence_stage_runs SET status='interrupted',finished_at=CURRENT_TIMESTAMP,error_text=COALESCE(error_text,'process stopped before stage start') WHERE stage_name=? AND status='pending' AND started_at < datetime('now','-30 minutes')",(stage_name,))
    live=conn.execute("SELECT stage_run_id,status FROM evidence_stage_runs WHERE stage_name=? AND status IN ('pending','running') ORDER BY stage_run_id DESC LIMIT 1",(stage_name,)).fetchone()
    if live:
        conn.rollback(); conn.close()
        raise RuntimeError(f"stage {stage_name!r} already has active attempt {live[0]} ({live[1]})")
    prior=conn.execute("SELECT stage_run_id,attempt_number,status FROM evidence_stage_runs WHERE stage_name=? ORDER BY stage_run_id DESC LIMIT 1",(stage_name,)).fetchone()
    retry_of=None; attempt=1
    if prior:
        prior_id,prior_attempt,prior_status=prior
        attempt=(prior_attempt or 1)+1
        if prior_status in ('failed','interrupted','retryable'):
            retry_of=prior_id
            conn.execute("UPDATE evidence_stage_runs SET status='retryable' WHERE stage_run_id=? AND status IN ('failed','interrupted')",(prior_id,))
    payload=json.dumps(metadata or {},sort_keys=True)
    cur=conn.execute("INSERT INTO evidence_stage_runs(stage_name,status,metadata_json,attempt_number,retry_of_stage_run_id) VALUES (?,'pending',?,?,?)",(stage_name,payload,attempt,retry_of))
    run=cur.lastrowid
    conn.commit()
    started=conn.execute("UPDATE evidence_stage_runs SET status='running' WHERE stage_run_id=? AND status='pending'",(run,))
    if started.rowcount != 1:
        conn.close(); raise RuntimeError(f"could not start pending stage attempt {run}")
    conn.commit(); conn.close(); return run


def finish_stage(db_path, run_id, metadata=None, error=None):
    conn=sqlite3.connect(db_path)
    if error is None:
        conn.execute("UPDATE evidence_stage_runs SET status='completed',finished_at=CURRENT_TIMESTAMP,metadata_json=? WHERE stage_run_id=? AND status='running'",(json.dumps(metadata or {},sort_keys=True),run_id))
    else:
        conn.execute("UPDATE evidence_stage_runs SET status='failed',finished_at=CURRENT_TIMESTAMP,error_text=? WHERE stage_run_id=? AND status='running'",(str(error),run_id))
    conn.commit(); conn.close()


def process_database(db_path=DB):
    """Run evidence processing and persist stage-level success/failure metadata."""
    run_id=begin_stage(db_path,"evidence_processing",{})
    try:
        result=_process_database(db_path)
        finish_stage(db_path,run_id,result)
        result["stage_run_id"]=run_id
    except Exception as error:
        finish_stage(db_path,run_id,error=repr(error))
        raise
    # Keep normalization independently retryable. A normalization error is
    # recorded on its own attempt and does not invalidate completed selection.
    try:
        result["normalization"]=normalize_selected_candidates(db_path)
    except Exception as error:
        result["normalization"]={"status":"failed","error":repr(error)}
    return result


if __name__=="__main__": print(process_database())

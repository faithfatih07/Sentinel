import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evidence_processing import (
    apply_migrations, candidate_key, normalize_confidence,
    process_database, quality_assessment,
)


PROJECT = Path(__file__).resolve().parent


class EvidenceLayerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sentinel-phase1-")
        self.db = Path(self.temp.name) / "sentinel.db"
        shutil.copy2(PROJECT / "sentinel.db", self.db)
        c=sqlite3.connect(self.db)
        c.execute("DROP VIEW IF EXISTS current_selected_matching_input")
        # Reset only the disposable test clone to the pre-layer schema.
        tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        if "evidence_normalizations_legacy" in tables:
            c.execute("DROP TABLE IF EXISTS evidence_normalizations")
            c.execute("ALTER TABLE evidence_normalizations_legacy RENAME TO evidence_normalizations")
        for table in ("evidence_associations","evidence_selection_decisions","evidence_candidates","evidence_selection_runs","evidence_stage_runs","evidence_stage_runs_legacy","evidence_schema_migrations"):
            c.execute(f"DROP TABLE IF EXISTS {table}")
        c.execute("DROP TRIGGER IF EXISTS evidence_candidate_raw_immutable")
        c.execute("DROP TRIGGER IF EXISTS evidence_candidate_no_delete")
        c.commit(); c.close()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        c = sqlite3.connect(self.db)
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def test_existing_66_sources_and_foreign_key_trace(self):
        result = process_database(self.db)
        c = self.connect()
        expected = c.execute("SELECT COUNT(*) FROM extracted_artifacts WHERE artifact_type IN ('ocr_text','transcript')").fetchone()[0]
        c.close()
        self.assertEqual(result["source_artifacts"], expected)
        c = self.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_candidates WHERE source_artifact_id IS NOT NULL").fetchone()[0], expected)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_candidates e LEFT JOIN extracted_artifacts a ON a.id=e.source_artifact_id LEFT JOIN media_assets m ON m.id=e.media_asset_id WHERE a.id IS NULL OR m.id IS NULL").fetchone()[0], 0)
        c.close()

    def test_repeated_processing_is_idempotent_and_selection_stable(self):
        process_database(self.db)
        c = self.connect()
        before = c.execute("SELECT candidate_id,selection_status FROM evidence_candidates ORDER BY candidate_id").fetchall()
        n = c.execute("SELECT COUNT(*) FROM evidence_candidates").fetchone()[0]
        c.close()
        process_database(self.db)
        c = self.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_candidates").fetchone()[0], n)
        self.assertEqual(c.execute("SELECT candidate_id,selection_status FROM evidence_candidates ORDER BY candidate_id").fetchall(), before)
        c.close()

    def test_normalization_is_selected_only_versioned_and_idempotent(self):
        process_database(self.db)
        c=self.connect()
        selected=c.execute("select count(*) from evidence_candidates where selection_status='selected' and plain_text!=''").fetchone()[0]
        normalized=c.execute("select count(*) from evidence_normalizations where normalization_version='nfkc_tr_v2'").fetchone()[0]
        self.assertEqual(normalized,selected)
        self.assertGreater(c.execute("select count(*) from evidence_normalizations_legacy").fetchone()[0],0)
        mismatches=c.execute("select count(*) from evidence_candidates c join extracted_artifacts e on e.id=c.source_artifact_id where c.raw_output!=coalesce(e.text_content,'')").fetchone()[0]
        self.assertEqual(mismatches,0)
        c.close()
        process_database(self.db)
        c=self.connect()
        self.assertEqual(c.execute("select count(*) from evidence_normalizations where normalization_version='nfkc_tr_v2'").fetchone()[0],selected)
        c.close()

    def test_changed_configuration_has_distinct_identity(self):
        a = candidate_key(10, 3, "visual", "tesseract", "psm6", "5.3", "tur+eng", '{"psm":6}')
        b = candidate_key(10, 3, "visual", "tesseract", "psm11", "5.3", "tur+eng", '{"psm":11}')
        self.assertNotEqual(a, b)

    def test_missing_frame_placeholder_stable_and_timestamp_null(self):
        c = self.connect()
        c.execute("INSERT INTO media_assets(advertisement_id,parent_asset_id,asset_type,local_path) VALUES (5,1,'image','oddname.jpg')")
        frame_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit(); c.close()
        process_database(self.db)
        c = self.connect()
        first = c.execute("SELECT candidate_id,frame_timestamp_seconds FROM evidence_candidates WHERE frame_asset_id=? AND selection_status='missing'",(frame_id,)).fetchall()
        c.close()
        process_database(self.db)
        c = self.connect()
        again = c.execute("SELECT candidate_id,frame_timestamp_seconds FROM evidence_candidates WHERE frame_asset_id=? AND selection_status='missing'",(frame_id,)).fetchall()
        self.assertEqual(first, again)
        self.assertEqual(len(again), 1)
        self.assertIsNone(again[0][1])
        c.close()

    def test_missing_transcript_timestamp_is_flagged_and_not_associated(self):
        c = self.connect(); c.execute("INSERT INTO extracted_artifacts(advertisement_id,media_asset_id,artifact_type,text_content,start_seconds,end_seconds,extraction_method,model_name) VALUES (5,2,'transcript','missing timestamp example',NULL,NULL,'whisper.cpp','test')"); missing_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit(); c.close()
        process_database(self.db)
        c = self.connect()
        candidate=c.execute("SELECT candidate_id,quality_status FROM evidence_candidates WHERE source_artifact_id=?",(missing_id,)).fetchone()
        self.assertEqual(candidate[1], "missing_or_invalid_timestamp")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_associations WHERE transcript_candidate_id=?",(candidate[0],)).fetchone()[0], 0)
        c.close()

    def test_invalid_media_type_is_flagged(self):
        c=self.connect(); c.execute("UPDATE extracted_artifacts SET media_asset_id=2 WHERE id=48"); c.commit(); c.close()
        process_database(self.db)
        c=self.connect()
        status=c.execute("SELECT quality_status FROM evidence_candidates WHERE source_artifact_id=48").fetchone()[0]
        self.assertEqual(status,"invalid_media_association")
        c.close()

    def test_overlapping_intervals_and_half_open_boundary(self):
        c = self.connect()
        # Frame timestamps 0..19; overlapping intervals at t=2 both associate.
        c.execute("INSERT INTO extracted_artifacts(advertisement_id,media_asset_id,artifact_type,text_content,start_seconds,end_seconds,extraction_method,model_name) VALUES (5,2,'transcript','overlap one',0,3,'whisper.cpp','test')")
        c.execute("INSERT INTO extracted_artifacts(advertisement_id,media_asset_id,artifact_type,text_content,start_seconds,end_seconds,extraction_method,model_name) VALUES (5,2,'transcript','overlap two',1,4,'whisper.cpp','test')")
        c.commit(); c.close()
        process_database(self.db)
        c = self.connect()
        at_t2 = c.execute("SELECT a.plain_text FROM evidence_associations x JOIN evidence_candidates a ON a.candidate_id=x.transcript_candidate_id WHERE x.frame_asset_id=5 AND a.plain_text IN ('overlap one','overlap two')").fetchall()
        self.assertEqual({x[0] for x in at_t2},{'overlap one','overlap two'})
        # t=3 is excluded from [0,3), but included in [1,4).
        at_t3 = c.execute("SELECT a.plain_text FROM evidence_associations x JOIN evidence_candidates a ON a.candidate_id=x.transcript_candidate_id WHERE x.frame_asset_id=6 AND a.plain_text IN ('overlap one','overlap two')").fetchall()
        self.assertEqual([x[0] for x in at_t3],['overlap two'])
        c.close()

    def test_duplicate_text_candidates_are_both_preserved(self):
        c=self.connect()
        baseline=c.execute("SELECT COUNT(*) FROM extracted_artifacts WHERE artifact_type IN ('ocr_text','transcript')").fetchone()[0]
        base=c.execute("SELECT * FROM extracted_artifacts WHERE id=48").fetchone()
        cols=[x[1] for x in c.execute("PRAGMA table_info(extracted_artifacts)")]
        values=list(base); values[cols.index('id')]=None; values[cols.index('model_name')]='duplicate config'
        c.execute(f"INSERT INTO extracted_artifacts ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",values)
        c.commit(); c.close()
        result=process_database(self.db)
        c=self.connect()
        self.assertEqual(result['source_artifacts'],baseline+1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_candidates WHERE frame_asset_id=3 AND raw_output=(SELECT text_content FROM extracted_artifacts WHERE id=48)").fetchone()[0],2)
        c.close()

    def test_contaminated_tsv_quarantined_from_plain_text(self):
        status,score,parts=quality_assessment("5 1 1 2 10 949 604 3 4 43.7 5 1 1 2 11 1344 591 29 53 80.1 5 1 1 2 12 100 200 4 5 40.0", "visual")
        self.assertEqual(status,"contaminated")

    def test_confidence_scales_and_missing_values(self):
        self.assertEqual(normalize_confidence(75,"visual"),(75.0,"percent_0_100",.75))
        self.assertEqual(normalize_confidence(.8,"audio"),(.8,"unit_interval_0_1",.8))
        self.assertEqual(normalize_confidence(.8,"audio","token_probability_mean_0_1"),(.8,"token_probability_mean_0_1",.8))
        self.assertEqual(normalize_confidence(None,"audio"),(None,"missing",None))

    def test_migration_version_and_existing_schema_compatibility(self):
        c=self.connect(); apply_migrations(c); apply_migrations(c)
        self.assertEqual(c.execute("SELECT MAX(version) FROM evidence_schema_migrations").fetchone()[0],10)
        self.assertIn("text_content",[r[1] for r in c.execute("PRAGMA table_info(extracted_artifacts)")])
        c.close()

    def test_failed_processing_stage_is_recorded(self):
        c=self.connect(); c.execute("UPDATE advertisements SET url='not-the-test-ad' WHERE url='local-test://sentinel/video-sample-001'"); c.commit(); c.close()
        with self.assertRaises(RuntimeError):
            process_database(self.db)
        c=self.connect()
        self.assertEqual(c.execute("SELECT status FROM evidence_stage_runs ORDER BY stage_run_id DESC LIMIT 1").fetchone()[0],"failed")
        c.close()

    def test_matching_view_excludes_alternatives_contamination_low_quality_and_stale_selection(self):
        from evidence_processing import normalize_selected_candidates
        process_database(self.db)
        c=self.connect()
        alt=c.execute("SELECT candidate_id,source_artifact_id,plain_text,advertisement_id,modality FROM evidence_candidates WHERE selection_status='alternative' AND source_artifact_id IS NOT NULL LIMIT 1").fetchone()
        self.assertIsNotNone(alt)
        # A historical normalized row for an alternative remains in the legacy
        # table, but must never become an active matcher input.
        source_id,plain=alt[1],alt[2]
        normalized=' '.join((plain or '').casefold().split()) or 'legacy alternative'
        import hashlib
        c.execute("INSERT OR IGNORE INTO normalized_artifacts(source_artifact_id,advertisement_id,modality,original_text,normalized_text,normalized_hash,normalization_version) VALUES (?,?,?,?,?,?,?)",
                  (source_id,alt[3],alt[4],plain or '',normalized,hashlib.sha256(normalized.encode()).hexdigest(),'legacy'))
        c.commit()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM current_selected_matching_input WHERE candidate_id=?",(alt[0],)).fetchone()[0],0)

        # Add contaminated and low-quality OCR source artifacts with valid frame
        # lineage; neither is eligible for the current selection view.
        ad=c.execute("SELECT id FROM advertisements WHERE url='local-test://sentinel/video-sample-001'").fetchone()[0]
        frames=c.execute("SELECT id,local_path FROM media_assets WHERE advertisement_id=? AND asset_type='image' ORDER BY id LIMIT 2",(ad,)).fetchall()
        self.assertEqual(len(frames),2)
        import re
        for frame,text in zip(frames,["level left top conf text 5 1 2 3 4 50.0", "x"]):
            number=re.search(r"frame_(\d+)\.jpg$",frame[1]).group(1)
            timestamp=int(number)-1
            c.execute("INSERT INTO extracted_artifacts(advertisement_id,media_asset_id,artifact_type,text_content,start_seconds,end_seconds,extraction_method,model_name) VALUES (?,?, 'ocr_text', ?,?,?, 'tesseract','test')",
                      (ad,frame[0],text,timestamp,timestamp))
        c.commit(); c.close()
        process_database(self.db)
        c=self.connect()
        states=c.execute("SELECT quality_status,COUNT(*) FROM evidence_candidates WHERE engine='tesseract' AND model='test' GROUP BY quality_status").fetchall()
        self.assertEqual({x[0] for x in states},{'contaminated','low_quality'})
        self.assertEqual(c.execute("SELECT COUNT(*) FROM current_selected_matching_input WHERE source_artifact_id IN (SELECT id FROM extracted_artifacts WHERE model_name='test')").fetchone()[0],0)

        selected=c.execute("SELECT candidate_id FROM evidence_candidates WHERE selection_status='selected' AND source_artifact_id IS NOT NULL LIMIT 1").fetchone()[0]
        self.assertEqual(c.execute("SELECT COUNT(*) FROM current_selected_matching_input WHERE candidate_id=?",(selected,)).fetchone()[0],1)
        c.execute("UPDATE evidence_candidates SET selection_status='alternative' WHERE candidate_id=?",(selected,))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM current_selected_matching_input WHERE candidate_id=?",(selected,)).fetchone()[0],0)
        c.close()

    def test_reprocessing_deselects_candidate_missing_from_current_source_rows(self):
        process_database(self.db)
        c=self.connect()
        candidate=c.execute("SELECT candidate_id,source_artifact_id FROM evidence_candidates WHERE selection_status='selected' AND source_artifact_id IS NOT NULL LIMIT 1").fetchone()
        c.execute("UPDATE extracted_artifacts SET artifact_type='metadata' WHERE id=?",(candidate[1],))
        c.commit(); c.close()
        process_database(self.db)
        c=self.connect()
        row=c.execute("SELECT selection_status,selection_reason FROM evidence_candidates WHERE candidate_id=?",(candidate[0],)).fetchone()
        self.assertEqual(row[0],'alternative')
        self.assertIn('absent from current selection run',row[1])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM current_selected_matching_input WHERE candidate_id=?",(candidate[0],)).fetchone()[0],0)
        c.close()

    def test_normalization_failure_does_not_repeat_completed_selection_and_retry_is_audited(self):
        from evidence_processing import begin_stage, finish_stage
        run=begin_stage(self.db,'normalization',{'test':'fault'})
        finish_stage(self.db,run,error='simulated normalization failure')
        retry=begin_stage(self.db,'normalization',{'test':'retry'})
        c=self.connect()
        row=c.execute("SELECT attempt_number,retry_of_stage_run_id FROM evidence_stage_runs WHERE stage_run_id=?",(retry,)).fetchone()
        self.assertEqual(row,(2,run))
        original=c.execute("SELECT status,error_text FROM evidence_stage_runs WHERE stage_run_id=?",(run,)).fetchone()
        self.assertEqual(original,('retryable','simulated normalization failure'))
        c.execute("UPDATE evidence_stage_runs SET status='completed',finished_at=CURRENT_TIMESTAMP WHERE stage_run_id=?",(retry,))
        c.commit()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_stage_runs WHERE stage_name='evidence_processing'").fetchone()[0],0)
        c.close()

    def test_interrupted_stage_recovery_and_active_run_is_not_reset(self):
        from evidence_processing import begin_stage
        first=begin_stage(self.db,'recoverable',{'attempt':'first'})
        c=self.connect()
        c.execute("UPDATE evidence_stage_runs SET started_at=datetime('now','-1 hour') WHERE stage_run_id=?",(first,))
        c.commit(); c.close()
        second=begin_stage(self.db,'recoverable',{'attempt':'retry'})
        c=self.connect()
        original=c.execute("SELECT status,error_text FROM evidence_stage_runs WHERE stage_run_id=?",(first,)).fetchone()
        retry=c.execute("SELECT attempt_number,retry_of_stage_run_id FROM evidence_stage_runs WHERE stage_run_id=?",(second,)).fetchone()
        self.assertEqual(original,('retryable','process disappeared before stage completion'))
        self.assertEqual(retry,(2,first))
        c.close()
        with self.assertRaisesRegex(RuntimeError,'active attempt'):
            begin_stage(self.db,'recoverable',{'attempt':'should-not-start'})
        c=self.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_stage_runs WHERE stage_name='recoverable'").fetchone()[0],2)
        c.close()

    def test_durable_pending_attempt_recovers_after_interruption(self):
        from evidence_processing import begin_stage
        c=self.connect()
        apply_migrations(c)
        c.execute("INSERT INTO evidence_stage_runs(stage_name,status,started_at,metadata_json,attempt_number) VALUES ('pending_recovery','pending',datetime('now','-1 hour'),'{}',1)")
        old=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit(); c.close()
        new=begin_stage(self.db,'pending_recovery',{'retry':True})
        c=self.connect()
        previous=c.execute("SELECT status,error_text FROM evidence_stage_runs WHERE stage_run_id=?",(old,)).fetchone()
        attempt=c.execute("SELECT status,attempt_number,retry_of_stage_run_id FROM evidence_stage_runs WHERE stage_run_id=?",(new,)).fetchone()
        self.assertEqual(previous,('retryable','process stopped before stage start'))
        self.assertEqual(attempt,('running',2,old))
        c.close()

    def test_v7_imports_archived_stage_history(self):
        c=self.connect()
        c.execute("CREATE TABLE evidence_schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        c.executemany("INSERT INTO evidence_schema_migrations(version) VALUES (?)",[(n,) for n in range(1,7)])
        c.execute("""CREATE TABLE evidence_stage_runs(stage_run_id INTEGER PRIMARY KEY AUTOINCREMENT,stage_name TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','interrupted','retryable')),
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,finished_at TEXT,error_text TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',
          attempt_number INTEGER NOT NULL DEFAULT 1,retry_of_stage_run_id INTEGER)""")
        c.execute("""CREATE TABLE evidence_stage_runs_legacy(stage_run_id INTEGER PRIMARY KEY,stage_name TEXT,status TEXT,
          started_at TEXT,finished_at TEXT,error_text TEXT,metadata_json TEXT)""")
        c.execute("INSERT INTO evidence_stage_runs_legacy VALUES (77,'ocr_ingestion','succeeded','2026-01-01','2026-01-02',NULL,'{\"frames\":20}')")
        c.commit()
        apply_migrations(c)
        migrated=c.execute("SELECT status,metadata_json FROM evidence_stage_runs WHERE json_extract(metadata_json,'$.migrated_legacy_stage_run_id')=77").fetchone()
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated[0],'completed')
        self.assertEqual(json.loads(migrated[1])['legacy_metadata'],{'frames':20})
        self.assertEqual(c.execute('SELECT COUNT(*) FROM evidence_stage_runs_legacy WHERE stage_run_id=77').fetchone()[0],1)
        c.close()

    def test_normalization_failure_can_retry_without_rerunning_selection(self):
        from evidence_processing import normalize_selected_candidates
        process_database(self.db)
        c=self.connect()
        before_candidates=c.execute("SELECT COUNT(*) FROM evidence_candidates").fetchone()[0]
        before_selection_runs=c.execute("SELECT COUNT(*) FROM evidence_selection_runs").fetchone()[0]
        c.execute("CREATE TRIGGER test_fail_normalization BEFORE INSERT ON evidence_normalizations BEGIN SELECT RAISE(ABORT,'injected normalization failure'); END")
        c.commit(); c.close()
        with self.assertRaises(sqlite3.IntegrityError):
            normalize_selected_candidates(self.db)
        c=self.connect(); c.execute("DROP TRIGGER test_fail_normalization"); c.commit(); c.close()
        recovered=normalize_selected_candidates(self.db)
        self.assertTrue(recovered['run_id'])
        c=self.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_candidates").fetchone()[0],before_candidates)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_selection_runs").fetchone()[0],before_selection_runs)
        failure=c.execute("SELECT status,error_text FROM evidence_stage_runs WHERE stage_name='normalization' AND error_text LIKE '%injected normalization failure%' ORDER BY stage_run_id LIMIT 1").fetchone()
        self.assertEqual(failure,('retryable',"IntegrityError('injected normalization failure')"))
        self.assertEqual(c.execute("SELECT status FROM evidence_stage_runs WHERE stage_run_id=?",(recovered['run_id'],)).fetchone()[0],'completed')
        c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

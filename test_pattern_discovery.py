import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evidence_processing import apply_migrations
from pattern_discovery import export_ai_bundle, get_candidate, import_ai_suggestions, list_candidates, review_candidate, run_local_discovery
from pattern_discovery import WORD_RE
from pattern_discovery_ai import build_ai_request, run_ai_discovery
from pattern_discovery_providers import ProviderConfig, ProviderConfigurationError, load_provider

PROJECT=Path(__file__).resolve().parent


class PatternDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sentinel-discovery-')
        self.db=Path(self.temp.name)/'sentinel.db'
        shutil.copy2(PROJECT/'sentinel.db',self.db)
        c=sqlite3.connect(self.db);c.execute('PRAGMA foreign_keys=ON');apply_migrations(c);c.close()
        c=sqlite3.connect(self.db);c.execute('PRAGMA foreign_keys=ON')
        for table in ('pattern_discovery_candidate_evidence','pattern_discovery_candidate_runs','pattern_discovery_candidates','pattern_discovery_runs'):
            c.execute(f'DELETE FROM {table}')
        c.commit();c.close()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        c=sqlite3.connect(self.db);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

    def ai_suggestion(self,text='agricultural distribution advisory',language='en',candidate_type='service_description'):
        c=self.connect()
        source=c.execute('SELECT candidate_id,source_artifact_id,original_text FROM current_selected_matching_input ORDER BY source_artifact_id LIMIT 1').fetchone()
        c.close()
        excerpt=source['original_text'].splitlines()[0]
        return {'proposals':[{'candidate_name':text,'language':language,'candidate_category':candidate_type,
          'description':'Human-review test candidate','confidence':0.61,'confidence_basis':'one exact selected excerpt; low cross-source support',
          'reasoning':'The cited wording may describe a reusable service expression, but one source is insufficient to establish recurrence.',
          'novelty_type':'new_pattern','target_pattern_id':None,'suggested_category_key':'service',
          'supporting_evidence':[{'source_candidate_id':source['candidate_id'],'source_artifact_id':source['source_artifact_id'],'exact_source_excerpt':excerpt}]}]}

    def mock_response(self, request, name='farm portfolio guidance'):
        source=request['selected_evidence'][0]
        return {'proposals':[{'candidate_name':name,'language':source['language'],'candidate_category':'service_description',
          'description':'Mocked provider suggestion','confidence':0.72,'confidence_basis':'One cited selected source; uncalibrated.',
          'reasoning':'Potentially reusable service wording; needs human review.', 'novelty_type':'new_pattern','target_pattern_id':None,
          'suggested_category_key':'service','supporting_evidence':[{'source_candidate_id':source['source_candidate_id'],
            'source_artifact_id':source['source_artifact_id'],'exact_source_excerpt':source['raw_text'].splitlines()[0]}]}]}

    def test_local_mining_uses_current_selection_and_does_not_publish(self):
        c=self.connect()
        before_patterns=c.execute('SELECT id,canonical_name,description,is_active FROM patterns ORDER BY id').fetchall()
        allowed={r[0] for r in c.execute('SELECT candidate_id FROM current_selected_matching_input')}
        c.close()
        result=run_local_discovery(self.db)
        self.assertEqual(result['selected_evidence_count'],len(allowed))
        self.assertGreater(result['discovered_candidates'],0)
        c=self.connect()
        after_patterns=c.execute('SELECT id,canonical_name,description,is_active FROM patterns ORDER BY id').fetchall()
        self.assertEqual(before_patterns,after_patterns)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE discovery_status='approved'").fetchone()[0],0)
        overcommon_names=c.execute("""SELECT COUNT(*) FROM pattern_discovery_candidates d WHERE candidate_type='company_brand'
          AND (SELECT COUNT(DISTINCT source_candidate_id) FROM pattern_discovery_candidate_evidence e WHERE e.candidate_id=d.candidate_id)>6""").fetchone()[0]
        self.assertEqual(overcommon_names,0)
        tokens={}
        for text,source_id in c.execute('SELECT normalized_text,candidate_id FROM current_selected_matching_input'):
            for token in set(WORD_RE.findall(text or '')):
                tokens.setdefault(token,set()).add(source_id)
        for (normalized,) in c.execute("SELECT normalized_text FROM pattern_discovery_candidates WHERE candidate_type='company_brand' AND is_current=1"):
            self.assertTrue(all(len(tokens.get(token,set()))<=max(4,int(len(allowed)*0.12)) for token in normalized.split()))
        refs={r[0] for r in c.execute('SELECT DISTINCT source_candidate_id FROM pattern_discovery_candidate_evidence')}
        self.assertTrue(refs.issubset(allowed))
        self.assertEqual(c.execute('SELECT status FROM pattern_discovery_runs WHERE run_id=?',(result['run_id'],)).fetchone()[0],'completed')
        c.close()

    def test_repeat_discovery_reuses_candidates_and_adds_run_audit(self):
        first=run_local_discovery(self.db)
        c=self.connect();count=c.execute('SELECT COUNT(*) FROM pattern_discovery_candidates').fetchone()[0];c.close()
        second=run_local_discovery(self.db)
        c=self.connect()
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidates').fetchone()[0],count)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_runs').fetchone()[0],2)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_runs WHERE run_id=?',(first['run_id'],)).fetchone()[0],first['discovered_candidates'])
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_runs WHERE run_id=?',(second['run_id'],)).fetchone()[0],second['discovered_candidates'])
        c.close()

    def test_ai_import_stays_pending_until_explicit_human_approval(self):
        suggestion=self.ai_suggestion()
        c=self.connect();before=c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0];c.close()
        imported=import_ai_suggestions(suggestion,self.db)
        c=self.connect()
        candidate=c.execute('SELECT candidate_id,discovery_status,proposed_by FROM pattern_discovery_candidates').fetchone()
        self.assertEqual(tuple(candidate)[1:],('pending_review','ai_assisted'))
        self.assertEqual(c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0],before)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_evidence').fetchone()[0],1)
        c.close()
        shown=get_candidate(candidate['candidate_id'],self.db)
        self.assertEqual(shown['discovery_status'],'pending_review')
        self.assertEqual(len(shown['evidence']),1)
        self.assertEqual(len(list_candidates(self.db,'pending_review')),1)
        review=review_candidate(candidate['candidate_id'],'approved','reviewer@example.test','Reviewed exact source and distinct value.',self.db)
        self.assertEqual(review['status'],'approved')
        self.assertIsNotNone(review['approved_pattern_id'])
        c=self.connect()
        self.assertEqual(c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0],before+1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_variants WHERE pattern_id=?',(review['approved_pattern_id'],)).fetchone()[0],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_reviews WHERE candidate_id=?',(candidate['candidate_id'],)).fetchone()[0],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidates WHERE discovery_status="approved"').fetchone()[0],1)
        self.assertEqual(imported['imported'],1)
        c.close()

    def test_exact_duplicate_is_flagged_and_cannot_be_approved(self):
        result=import_ai_suggestions(self.ai_suggestion('investment advisory','en','recurring_phrase'),self.db)
        c=self.connect();candidate=c.execute('SELECT candidate_id,discovery_status,duplicate_pattern_id FROM pattern_discovery_candidates').fetchone();before=c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0];c.close()
        self.assertEqual(candidate['discovery_status'],'duplicate')
        self.assertIsNotNone(candidate['duplicate_pattern_id'])
        with self.assertRaisesRegex(ValueError,'exact duplicate'):
            review_candidate(candidate['candidate_id'],'approved','reviewer@example.test','duplicate test',self.db)
        c=self.connect();self.assertEqual(c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0],before);c.close()
        self.assertEqual(result['imported'],1)

    def test_ai_import_rejects_stale_or_fabricated_source_references(self):
        suggestion=self.ai_suggestion()
        suggestion['proposals'][0]['supporting_evidence'][0]['source_artifact_id']=-1
        with self.assertRaisesRegex(ValueError,'outside current selected evidence'):
            import_ai_suggestions(suggestion,self.db)
        suggestion=self.ai_suggestion()
        suggestion['proposals'][0]['supporting_evidence'][0]['exact_source_excerpt']='fabricated quote'
        with self.assertRaisesRegex(ValueError,'exactly occur'):
            import_ai_suggestions(suggestion,self.db)

    def test_provider_request_is_selected_evidence_and_active_library_only(self):
        request=build_ai_request(self.db)
        c=self.connect()
        allowed={r[0] for r in c.execute('SELECT candidate_id FROM current_selected_matching_input')}
        active={r[0] for r in c.execute('SELECT id FROM patterns WHERE is_active=1')}
        c.close()
        self.assertEqual({r['source_candidate_id'] for r in request['selected_evidence']},allowed)
        self.assertEqual({r['id'] for r in request['approved_pattern_library']},active)
        self.assertNotIn('previous_proposals',request)
        self.assertNotIn('api_key',request)

    def test_mock_provider_suggestion_persists_separately_without_approval(self):
        class MockProvider:
            def __init__(self): self.request=None
            def generate(self,request):
                self.request=request
                return self_outer.mock_response(request)
        self_outer=self
        c=self.connect();before=c.execute('SELECT id,canonical_name,is_active FROM patterns ORDER BY id').fetchall();c.close()
        provider=MockProvider()
        result=run_ai_discovery(self.db,provider,ProviderConfig(provider='mock',model='fixture'))
        repeated=run_ai_discovery(self.db,provider,ProviderConfig(provider='mock',model='fixture'))
        c=self.connect()
        after=c.execute('SELECT id,canonical_name,is_active FROM patterns ORDER BY id').fetchall()
        row=c.execute('SELECT novelty_type,reasoning,confidence_basis,discovery_status,proposed_by FROM pattern_discovery_candidates').fetchone()
        self.assertEqual(before,after)
        self.assertEqual(tuple(row),('new_pattern','Potentially reusable service wording; needs human review.','One cited selected source; uncalibrated.','pending_review','ai_assisted'))
        run_config=json.loads(c.execute('SELECT config_json FROM pattern_discovery_runs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(run_config['provider'],'mock')
        self.assertEqual(run_config['model'],'fixture')
        self.assertNotIn('api_key',run_config)
        self.assertNotIn('secret',json.dumps(run_config).lower())
        self.assertEqual(result['imported'],1)
        self.assertEqual(repeated['imported'],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by="ai_assisted"').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_runs').fetchone()[0],2)
        self.assertIsNotNone(provider.request)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_evidence').fetchone()[0],1)
        c.close()

    def test_ai_response_requires_all_fields_and_valid_target(self):
        suggestion=self.ai_suggestion()
        del suggestion['proposals'][0]['reasoning']
        with self.assertRaisesRegex(ValueError,'reasoning'):
            import_ai_suggestions(suggestion,self.db)
        suggestion=self.ai_suggestion()
        del suggestion['proposals'][0]['supporting_evidence']
        with self.assertRaisesRegex(ValueError,'supporting_evidence'):
            import_ai_suggestions(suggestion,self.db)
        suggestion=self.ai_suggestion()
        suggestion['proposals'][0]['novelty_type']='variant'
        suggestion['proposals'][0]['target_pattern_id']=999999
        with self.assertRaisesRegex(ValueError,'active approved target'):
            import_ai_suggestions(suggestion,self.db)

    def test_variant_requires_human_approval_and_adds_to_target_pattern(self):
        suggestion=self.ai_suggestion('specialized rural portfolio planning','en','service_description')
        c=self.connect();target=c.execute('SELECT id FROM patterns WHERE is_active=1 ORDER BY id LIMIT 1').fetchone()[0]
        pattern_count=c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0]
        variant_count=c.execute('SELECT COUNT(*) FROM pattern_variants WHERE pattern_id=?',(target,)).fetchone()[0];c.close()
        proposal=suggestion['proposals'][0];proposal['novelty_type']='variant';proposal['target_pattern_id']=target
        imported=import_ai_suggestions(suggestion,self.db)
        c=self.connect();candidate=c.execute('SELECT candidate_id,discovery_status FROM pattern_discovery_candidates').fetchone()
        self.assertEqual(candidate['discovery_status'],'pending_review');c.close()
        review_candidate(candidate['candidate_id'],'approved','reviewer@example.test','Reviewed as variant.',self.db)
        c=self.connect()
        self.assertEqual(c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0],pattern_count)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_variants WHERE pattern_id=?',(target,)).fetchone()[0],variant_count+1)
        c.close();self.assertEqual(imported['imported'],1)

    def test_provider_configuration_is_optional_and_secret_is_environment_named(self):
        disabled=ProviderConfig.from_environment({})
        self.assertEqual(disabled.provider,'disabled')
        with self.assertRaises(ProviderConfigurationError): load_provider(disabled,{})
        config=ProviderConfig.from_environment({'SENTINEL_DISCOVERY_PROVIDER':'mock','SENTINEL_DISCOVERY_MODEL':'fixture',
            'SENTINEL_DISCOVERY_API_KEY_ENV':'MOCK_SECRET'})
        self.assertEqual(config.api_key_env,'MOCK_SECRET')
        self.assertFalse(hasattr(config,'api_key'))
        with self.assertRaisesRegex(ProviderConfigurationError,'unset'):
            from pattern_discovery_providers import resolve_api_key
            resolve_api_key(config,{})

    def test_offline_ai_bundle_contains_only_current_selected_evidence(self):
        target=Path(self.temp.name)/'bundle.json'
        result=export_ai_bundle(self.db,target)
        bundle=json.loads(target.read_text(encoding='utf-8'))
        c=self.connect();allowed={r[0] for r in c.execute('SELECT candidate_id FROM current_selected_matching_input')};c.close()
        refs={r['source_candidate_id'] for r in bundle['evidence']}
        self.assertEqual(refs,allowed)
        self.assertEqual(result['selected_evidence_count'],len(allowed))
        self.assertEqual(bundle['metadata']['source'],'current_selected_matching_input')

    def test_v8_v10_migrations_upgrade_phase2_5_database_without_changing_library(self):
        legacy=self.db.parent/'phase2_5.db'
        shutil.copy2(PROJECT/'sentinel_pre_phase3_20261122.db',legacy)
        c=sqlite3.connect(legacy);c.execute('PRAGMA foreign_keys=ON')
        before_patterns=c.execute('SELECT id,canonical_name,description,is_active FROM patterns ORDER BY id').fetchall()
        before_variants=c.execute('SELECT id,pattern_id,language,variant_text FROM pattern_variants ORDER BY id').fetchall()
        before_inputs=c.execute('SELECT COUNT(*) FROM current_selected_matching_input').fetchone()[0]
        apply_migrations(c)
        self.assertEqual(c.execute('SELECT MAX(version) FROM evidence_schema_migrations').fetchone()[0],10)
        self.assertEqual(c.execute('SELECT id,canonical_name,description,is_active FROM patterns ORDER BY id').fetchall(),before_patterns)
        self.assertEqual(c.execute('SELECT id,pattern_id,language,variant_text FROM pattern_variants ORDER BY id').fetchall(),before_variants)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM current_selected_matching_input').fetchone()[0],before_inputs)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidates').fetchone()[0],0)
        c.close()

    def test_approval_is_blocked_if_supporting_evidence_is_no_longer_selected(self):
        import_ai_suggestions(self.ai_suggestion(),self.db)
        c=self.connect();candidate=c.execute('SELECT candidate_id FROM pattern_discovery_candidates').fetchone()[0]
        source=c.execute('SELECT source_candidate_id FROM pattern_discovery_candidate_evidence WHERE candidate_id=?',(candidate,)).fetchone()[0]
        c.execute("UPDATE evidence_candidates SET selection_status='alternative' WHERE candidate_id=?",(source,));c.commit();c.close()
        with self.assertRaisesRegex(ValueError,'no-longer-selected'):
            review_candidate(candidate,'approved','reviewer@example.test','stale test',self.db)


if __name__=='__main__':
    unittest.main(verbosity=2)

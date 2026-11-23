import json
import shutil
import sqlite3
import tempfile
import unittest
from io import BytesIO
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from evidence_processing import apply_migrations
from pattern_discovery import export_ai_bundle, get_candidate, import_ai_suggestions, list_candidates, review_candidate, run_local_discovery
from pattern_discovery import WORD_RE
from pattern_discovery_ai import build_ai_request, run_ai_discovery, _summarize_response
from pattern_discovery_providers import ProviderConfig, ProviderConfigurationError, ProviderRequestError, load_provider
from pattern_discovery_openai import OpenAIResponsesProvider
from pattern_discovery_gemini import GeminiGenerateContentProvider

PROJECT=Path(__file__).resolve().parent


class PatternDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sentinel-discovery-')
        self.db=Path(self.temp.name)/'sentinel.db'
        shutil.copy2(PROJECT/'sentinel.db',self.db)
        c=sqlite3.connect(self.db);c.execute('PRAGMA foreign_keys=ON');apply_migrations(c);c.close()
        c=sqlite3.connect(self.db);c.execute('PRAGMA foreign_keys=ON')
        # This is a disposable copy. Remove append-only guards only here so
        # seed review rows can be cleared before each isolated test case.
        c.execute('DROP TRIGGER IF EXISTS discovery_review_no_update')
        c.execute('DROP TRIGGER IF EXISTS discovery_review_no_delete')
        for table in ('pattern_discovery_reviews','pattern_discovery_candidate_evidence','pattern_discovery_candidate_runs','pattern_discovery_candidates','pattern_discovery_runs'):
            c.execute(f'DELETE FROM {table}')
        c.commit();c.close()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        c=sqlite3.connect(self.db);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

    def ai_suggestion(self,text=None,language='en',candidate_type='service_description'):
        c=self.connect()
        source=c.execute('SELECT candidate_id,source_artifact_id,original_text FROM current_selected_matching_input ORDER BY source_artifact_id LIMIT 1').fetchone()
        c.close()
        excerpt=source['original_text'].splitlines()[0]
        text=text or excerpt
        return {'proposals':[{'candidate_name':text,'language':language,'candidate_category':candidate_type,
          'description':'Human-review test candidate','confidence':0.61,'confidence_basis':'one exact selected excerpt; low cross-source support',
          'reasoning':'The cited wording may describe a reusable service expression, but one source is insufficient to establish recurrence.',
          'novelty_type':'new_pattern','target_pattern_id':None,'suggested_category_key':'service',
          'supporting_evidence':[{'source_candidate_id':source['candidate_id'],'source_artifact_id':source['source_artifact_id'],'exact_source_excerpt':excerpt}]}]}

    def mock_response(self, request, name=None):
        source=request['selected_evidence'][0]
        span=source['citation_spans'][0]
        name=name or span['exact_source_excerpt']
        return {'proposals':[{'candidate_name':name,'language':source['language'],'candidate_category':'service_description',
          'description':'Mocked provider suggestion','confidence':0.72,'confidence_basis':'One cited selected source; uncalibrated.',
          'reasoning':'Potentially reusable service wording; needs human review.', 'novelty_type':'new_pattern','target_pattern_id':None,
          'suggested_category_key':'service','supporting_evidence':[{'source_candidate_id':source['source_candidate_id'],
            'source_artifact_id':source['source_artifact_id'],'source_excerpt_id':span['source_excerpt_id'],
            'exact_source_excerpt':None}]}]}

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
        suggestion=self.ai_suggestion()
        c=self.connect()
        c.execute('INSERT INTO patterns(canonical_name,description,is_active) VALUES (?,?,1)',
            (suggestion['proposals'][0]['candidate_name'],'Fixture duplicate target'))
        c.commit();c.close()
        result=import_ai_suggestions(suggestion,self.db)
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

    def test_ai_import_resolves_span_id_and_rejects_fabricated_span(self):
        request=build_ai_request(self.db)
        source=request['selected_evidence'][0]
        span=source['citation_spans'][0]
        suggestion=self.mock_response(request)
        result=import_ai_suggestions(suggestion,self.db)
        self.assertEqual(result['imported'],1)
        c=self.connect()
        self.assertEqual(c.execute('SELECT evidence_excerpt FROM pattern_discovery_candidate_evidence').fetchone()[0],span['exact_source_excerpt'])
        c.close()

        suggestion=self.mock_response(build_ai_request(self.db),'fabricated span')
        suggestion['proposals'][0]['supporting_evidence'][0]['source_excerpt_id']='s999'
        with self.assertRaisesRegex(ValueError,'not present in the cited selected source'):
            import_ai_suggestions(suggestion,self.db)

    def test_ai_discovery_rejects_candidate_name_not_present_in_its_citations(self):
        request=build_ai_request(self.db)
        response=self.mock_response(request,'unsupported invented name')
        class MockProvider:
            def generate(self,unused): return response
        result=run_ai_discovery(self.db,MockProvider(),ProviderConfig(provider='mock',model='fixture'))
        self.assertEqual(result['response_summary']['proposal_count'],1)
        self.assertEqual(result['rejected'],1)
        self.assertEqual(result['imported'],0)
        c=self.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by='ai_assisted'").fetchone()[0],0)
        summary=json.loads(c.execute('SELECT config_json FROM pattern_discovery_runs WHERE run_id=?',(result['run_id'],)).fetchone()[0])['response_summary']
        self.assertEqual(summary['unsupported_name_rejections'],1)
        c.close()

    def test_citation_spans_are_stable_and_exact(self):
        from pattern_discovery import citation_spans
        raw='Title | second line. Third sentence!'
        spans=citation_spans(raw)
        self.assertEqual(spans,citation_spans(raw))
        self.assertEqual([span['source_excerpt_id'] for span in spans],['s001','s002','s003'])
        self.assertTrue(all(span['exact_source_excerpt'] in raw for span in spans))

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
        guidance=' '.join(request['rules']).lower()
        self.assertIn('do not require recurrence',guidance)
        self.assertIn('at least two distinct source artifacts',guidance)
        self.assertIn('channel or program titles',guidance)
        self.assertIn("keep the candidate in its source language",guidance)
        self.assertIn('choose a supplied source_excerpt_id',guidance)
        self.assertIn('span that contains the candidate_name itself',guidance)
        self.assertIn('only after checking all supplied evidence',guidance)
        for source in request['selected_evidence']:
            self.assertTrue(source['citation_spans'])
            self.assertTrue(all(span['exact_source_excerpt'] in source['raw_text'] for span in source['citation_spans']))

    def test_response_audit_summary_excludes_candidate_text_and_citations(self):
        response={'proposals':[{'candidate_name':'private phrase','candidate_category':'service_description',
            'supporting_evidence':[{'source_candidate_id':'candidate-secret','source_artifact_id':123,
                                    'exact_source_excerpt':'private evidence excerpt'}]}]}
        summary=_summarize_response(response)
        self.assertEqual(summary['parse_status'],'valid_json')
        self.assertEqual(summary['proposal_count'],1)
        self.assertEqual(summary['proposal_categories'],{'service_description':1})
        self.assertEqual(summary['citation_count'],1)
        self.assertNotIn('candidate_name',summary)
        self.assertNotIn('candidate-secret',json.dumps(summary))
        self.assertNotIn('private evidence excerpt',json.dumps(summary))

    def test_recurring_phrase_requires_two_distinct_artifact_citations(self):
        suggestion=self.ai_suggestion(None,'en','recurring_phrase')
        with self.assertRaisesRegex(ValueError,'at least two distinct source artifacts'):
            import_ai_suggestions(suggestion,self.db)
        c=self.connect()
        sources=c.execute('SELECT candidate_id,source_artifact_id,original_text FROM current_selected_matching_input ORDER BY source_artifact_id LIMIT 2').fetchall()
        c.close()
        self.assertEqual(len(sources),2)
        proposal=suggestion['proposals'][0]
        proposal['supporting_evidence']=[{
            'source_candidate_id':source['candidate_id'],
            'source_artifact_id':source['source_artifact_id'],
            'exact_source_excerpt':source['original_text'].splitlines()[0],
        } for source in sources]
        result=import_ai_suggestions(suggestion,self.db)
        self.assertEqual(result['imported'],1)
        candidate=list_candidates(self.db,'pending_review')[0]
        self.assertEqual(candidate['candidate_type'],'recurring_phrase')
        self.assertEqual(len(candidate['evidence']),2)

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
        self.assertEqual(run_config['response_summary']['proposal_count'],1)
        self.assertEqual(run_config['response_summary']['proposal_categories'],{'service_description':1})
        self.assertNotIn('candidate_name',json.dumps(run_config))
        self.assertNotIn('api_key',run_config)
        self.assertNotIn('secret',json.dumps(run_config).lower())
        self.assertEqual(result['imported'],1)
        self.assertEqual(repeated['imported'],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by="ai_assisted"').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_runs').fetchone()[0],2)
        self.assertIsNotNone(provider.request)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM pattern_discovery_candidate_evidence').fetchone()[0],1)
        expected_excerpt=provider.request['selected_evidence'][0]['citation_spans'][0]['exact_source_excerpt']
        self.assertEqual(c.execute('SELECT evidence_excerpt FROM pattern_discovery_candidate_evidence').fetchone()[0],expected_excerpt)
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
        suggestion=self.ai_suggestion(None,'en','service_description')
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

    def test_openai_adapter_sends_selected_evidence_and_structured_schema(self):
        request_data={}
        def opener(req,timeout):
            request_data['url']=req.full_url
            request_data['headers']=dict(req.header_items())
            request_data['timeout']=timeout
            request_data['payload']=json.loads(req.data.decode('utf-8'))
            text=json.dumps(self.mock_response(build_ai_request(self.db)))
            return BytesIO(json.dumps({'status':'completed','output_text':text}).encode('utf-8'))
        config=ProviderConfig(provider='openai',model='test-model',timeout_seconds=7)
        c=self.connect();pattern_count=c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0];c.close()
        provider=OpenAIResponsesProvider(config,environ={'OPENAI_API_KEY':'test-only-secret'},opener=opener)
        result=run_ai_discovery(self.db,provider,config)
        self.assertEqual(result['provider'],'openai')
        self.assertEqual(result['model'],'test-model')
        self.assertEqual(request_data['url'],'https://api.openai.com/v1/responses')
        self.assertEqual(request_data['timeout'],7)
        self.assertEqual(request_data['payload']['model'],'test-model')
        self.assertFalse(request_data['payload']['store'])
        self.assertEqual(request_data['payload']['text']['format']['type'],'json_schema')
        system_prompt=request_data['payload']['input'][0]['content'][0]['text']
        self.assertIn('source_excerpt_id',system_prompt)
        self.assertIn('contains candidate_name itself',system_prompt)
        sent=json.loads(request_data['payload']['input'][1]['content'][0]['text'])
        expected=build_ai_request(self.db)
        self.assertEqual(sent['selected_evidence'],expected['selected_evidence'])
        self.assertEqual(sent['approved_pattern_library'],expected['approved_pattern_library'])
        self.assertNotIn('test-only-secret',json.dumps(request_data['payload']))
        c=self.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by='ai_assisted' AND discovery_status='pending_review'").fetchone()[0],1)
        run_config=json.loads(c.execute('SELECT config_json FROM pattern_discovery_runs WHERE run_id=?',(result['run_id'],)).fetchone()[0])
        self.assertEqual({k:run_config[k] for k in ('mode','source','provider','model')},
            {'mode':'ai_assisted','source':'current_selected_matching_input','provider':'openai','model':'test-model'})
        self.assertEqual(run_config['response_summary']['proposal_count'],1)
        self.assertEqual(c.execute('SELECT status FROM pattern_discovery_runs WHERE run_id=?',(result['run_id'],)).fetchone()[0],'completed')
        self.assertEqual(c.execute('SELECT COUNT(*) FROM patterns').fetchone()[0],pattern_count)
        c.close()

    def test_openai_adapter_failures_are_audited_without_secret_or_response_body(self):
        config=ProviderConfig(provider='openai',model='test-model')
        def assert_failed(provider, expected_error):
            with self.assertRaises(Exception):
                run_ai_discovery(self.db,provider,config)
            c=self.connect()
            row=c.execute("SELECT status,error_text,config_json FROM pattern_discovery_runs ORDER BY rowid DESC LIMIT 1").fetchone()
            self.assertEqual(row['status'],'failed')
            self.assertIn(expected_error,row['error_text'])
            self.assertNotIn('test-only-secret',row['error_text']+row['config_json'])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by='ai_assisted'").fetchone()[0],0)
            c.close()

        invalid=self.mock_response(build_ai_request(self.db))
        invalid['proposals'][0]['supporting_evidence'][0]['source_candidate_id']='fabricated'
        assert_failed(type('BadCitationProvider',(),{'generate':lambda self,request: invalid})(),'outside current selected evidence')
        assert_failed(type('MalformedProvider',(),{'generate':lambda self,request: '{not-json'})(),'valid JSON')
        assert_failed(type('TimeoutProvider',(),{'generate':lambda self,request: (_ for _ in ()).throw(ProviderRequestError('OpenAI API request timed out'))})(),'timed out')
        assert_failed(type('FailureProvider',(),{'generate':lambda self,request: (_ for _ in ()).throw(ProviderRequestError('OpenAI API returned HTTP 503'))})(),'HTTP 503')

        malformed=OpenAIResponsesProvider(config,environ={'OPENAI_API_KEY':'test-only-secret'},
            opener=lambda req,timeout: BytesIO(b'{malformed response'))
        with self.assertRaisesRegex(ProviderRequestError,'malformed JSON'):
            malformed.generate(build_ai_request(self.db))
        timeout=OpenAIResponsesProvider(config,environ={'OPENAI_API_KEY':'test-only-secret'},
            opener=lambda req,timeout: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaisesRegex(ProviderRequestError,'timed out'):
            timeout.generate(build_ai_request(self.db))
        def failed_http(req,timeout):
            raise HTTPError(req.full_url,503,'failure',{},BytesIO(b'test-only-secret body'))
        http=OpenAIResponsesProvider(config,environ={'OPENAI_API_KEY':'test-only-secret'},opener=failed_http)
        with self.assertRaisesRegex(ProviderRequestError,'HTTP 503') as raised:
            http.generate(build_ai_request(self.db))
        self.assertNotIn('test-only-secret',str(raised.exception))

    def test_openai_requires_environment_key_and_fixed_key_variable(self):
        config=ProviderConfig.from_environment({'SENTINEL_DISCOVERY_PROVIDER':'openai','SENTINEL_DISCOVERY_MODEL':'test-model'})
        self.assertEqual(config.api_key_env,'OPENAI_API_KEY')
        with self.assertRaisesRegex(ProviderConfigurationError,'OPENAI_API_KEY.*unset'):
            load_provider(config,{})
        with self.assertRaisesRegex(ProviderConfigurationError,'requires OPENAI_API_KEY'):
            ProviderConfig.from_environment({'SENTINEL_DISCOVERY_PROVIDER':'openai','SENTINEL_DISCOVERY_MODEL':'test-model',
                'SENTINEL_DISCOVERY_API_KEY_ENV':'OTHER_SECRET'})

    def test_gemini_inference_verifies_model_and_persists_valid_pending_candidate(self):
        request_data=[]
        valid_response=json.dumps(self.mock_response(build_ai_request(self.db)))
        responses=[
            {'models':[{'name':'models/test-gemini','baseModelId':'test-gemini',
                'supportedGenerationMethods':['generateContent']} ]},
            {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':valid_response}]}}],
             'usageMetadata':{'promptTokenCount':1200,'candidatesTokenCount':90,'totalTokenCount':1290}},
        ]
        def opener(req,timeout):
            request_data.append((req,timeout))
            return BytesIO(json.dumps(responses.pop(0)).encode('utf-8'))
        config=ProviderConfig(provider='gemini',model='test-gemini',timeout_seconds=9,api_key_env='GEMINI_API_KEY')
        provider=GeminiGenerateContentProvider(config,environ={'GEMINI_API_KEY':'test-gemini-secret'},opener=opener)
        result=run_ai_discovery(self.db,provider,config)
        self.assertEqual(result['provider'],'gemini')
        self.assertEqual(result['model'],'test-gemini')
        self.assertEqual(result['imported'],1)
        self.assertEqual(result['provider_usage'],{'promptTokenCount':1200,'candidatesTokenCount':90,'totalTokenCount':1290})
        self.assertEqual(len(request_data),2)
        self.assertEqual(request_data[0][0].get_method(),'GET')
        self.assertIn('/models?',request_data[0][0].full_url)
        self.assertEqual(request_data[1][0].get_method(),'POST')
        self.assertTrue(request_data[1][0].full_url.endswith('/models/test-gemini:generateContent'))
        for req,timeout in request_data:
            self.assertEqual(timeout,9)
            self.assertNotIn('test-gemini-secret',req.full_url)
            self.assertIn('test-gemini-secret',req.get_header('X-goog-api-key'))
        payload=json.loads(request_data[1][0].data.decode('utf-8'))
        self.assertEqual(payload['generationConfig']['responseMimeType'],'application/json')
        self.assertEqual(payload['generationConfig']['responseJsonSchema'],build_ai_request(self.db)['response_schema'])
        system_prompt=payload['systemInstruction']['parts'][0]['text']
        self.assertIn('A clear single source is enough',system_prompt)
        self.assertIn('channel or program titles',system_prompt)
        self.assertIn('complete scan finds no source-supported candidate',system_prompt)
        sent=json.loads(payload['contents'][0]['parts'][0]['text'])
        expected=build_ai_request(self.db)
        self.assertEqual(sent['selected_evidence'],expected['selected_evidence'])
        self.assertEqual(sent['approved_pattern_library'],expected['approved_pattern_library'])
        self.assertNotIn('test-gemini-secret',json.dumps(payload))
        c=self.connect()
        candidate=c.execute("SELECT discovery_status,proposed_by FROM pattern_discovery_candidates WHERE proposed_by='ai_assisted'").fetchone()
        self.assertEqual(tuple(candidate),('pending_review','ai_assisted'))
        self.assertEqual(c.execute('SELECT status FROM pattern_discovery_runs WHERE run_id=?',(result['run_id'],)).fetchone()[0],'completed')
        c.close()

    def test_gemini_rejects_unavailable_model_before_inference(self):
        calls=[]
        provider=GeminiGenerateContentProvider(
            ProviderConfig(provider='gemini',model='missing-model',api_key_env='GEMINI_API_KEY'),
            environ={'GEMINI_API_KEY':'fixture'},
            opener=lambda req,timeout: (calls.append(req) or BytesIO(json.dumps({'models':[
                {'name':'models/other-model','supportedGenerationMethods':['generateContent']}]}).encode('utf-8'))),
        )
        with self.assertRaisesRegex(ProviderConfigurationError,'not available'):
            run_ai_discovery(self.db,provider,provider.config)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].get_method(),'GET')
        c=self.connect()
        run=c.execute('SELECT status,error_text FROM pattern_discovery_runs ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(run['status'],'failed')
        self.assertIn('missing-model',run['error_text'])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by='ai_assisted'").fetchone()[0],0)
        c.close()

    def test_ai_cli_reports_provider_errors_without_traceback(self):
        from pattern_discovery import main
        stderr=StringIO()
        with patch('sys.argv',['pattern_discovery.py','ai']), \
                patch('pattern_discovery_ai.run_ai_discovery',side_effect=ProviderRequestError('Gemini model access denied (HTTP 404)')):
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code,2)
        self.assertIn('Gemini model access denied',stderr.getvalue())
        self.assertNotIn('Traceback',stderr.getvalue())

    def test_gemini_handles_invalid_key_rate_limit_timeout_and_malformed_response(self):
        config=ProviderConfig(provider='gemini',model='test-gemini',api_key_env='GEMINI_API_KEY')
        def provider_for(opener):
            return GeminiGenerateContentProvider(config,environ={'GEMINI_API_KEY':'fixture-secret'},opener=opener)
        def http_error(code,body):
            def opener(req,timeout):
                raise HTTPError(req.full_url,code,'provider failure',{},BytesIO(body))
            return opener
        invalid=provider_for(http_error(400,b'{"error":{"status":"INVALID_ARGUMENT","message":"API key not valid. fixture-secret"}}'))
        with self.assertRaisesRegex(ProviderRequestError,'API key is invalid') as error:
            invalid.generate(build_ai_request(self.db))
        self.assertNotIn('fixture-secret',str(error.exception))
        limited=provider_for(http_error(429,b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'))
        with self.assertRaisesRegex(ProviderRequestError,'rate limit or quota'):
            limited.generate(build_ai_request(self.db))
        timed_out=provider_for(lambda req,timeout: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaisesRegex(ProviderRequestError,'timed out'):
            timed_out.generate(build_ai_request(self.db))
        malformed=provider_for(lambda req,timeout: BytesIO(b'not-json'))
        with self.assertRaisesRegex(ProviderRequestError,'malformed JSON'):
            malformed.generate(build_ai_request(self.db))
        model_unavailable=provider_for(http_error(404,b'{"error":{"status":"NOT_FOUND","message":"models/test-gemini is not found for API version v1beta, or is not supported for generateContent"}}'))
        with self.assertRaisesRegex(ProviderRequestError,'listed but is unavailable or unsupported'):
            model_unavailable.generate(build_ai_request(self.db))

    def test_gemini_configuration_reads_provider_and_key_from_project_env_file(self):
        from pattern_discovery_providers import project_environment
        env_path=Path(self.temp.name)/'.env'
        env_path.write_text('GEMINI_API_KEY=fixture-only\nSENTINEL_DISCOVERY_PROVIDER=gemini\nSENTINEL_DISCOVERY_MODEL=fixture-model\n',encoding='utf-8')
        config=ProviderConfig.from_environment(project_environment(environ={},dotenv_path=env_path))
        self.assertEqual(config.api_key_env,'GEMINI_API_KEY')
        self.assertEqual(config.provider,'gemini')
        self.assertEqual(project_environment({'GEMINI_API_KEY':'fixture-only'})['GEMINI_API_KEY'],'fixture-only')
        with self.assertRaisesRegex(ProviderConfigurationError,'requires GEMINI_API_KEY'):
            ProviderConfig.from_environment({'SENTINEL_DISCOVERY_PROVIDER':'gemini',
                'SENTINEL_DISCOVERY_MODEL':'fixture-model','SENTINEL_DISCOVERY_API_KEY_ENV':'OTHER_KEY'})

    def test_missing_openai_key_is_audited_without_attempting_inference(self):
        config=ProviderConfig(provider='openai',model='test-model',api_key_env='OPENAI_API_KEY')
        with self.assertRaisesRegex(ProviderConfigurationError,'OPENAI_API_KEY.*unset'):
            run_ai_discovery(self.db,config=config,environ={})
        c=self.connect()
        run=c.execute('SELECT status,error_text,config_json FROM pattern_discovery_runs ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(run['status'],'failed')
        self.assertIn('OPENAI_API_KEY',run['error_text'])
        self.assertNotIn('secret',run['config_json'].lower())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE proposed_by='ai_assisted'").fetchone()[0],0)
        c.close()

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

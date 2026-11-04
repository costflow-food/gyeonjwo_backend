"""Offline relocation regressions; audit/network guards exist only in a child."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_FLAG = '--crawl-worker'
_TAG = 'CRAWL_MOVE_RESULT='


def _worker():
    if __name__ != '__main__' or sys.argv[1:] != [_FLAG]:
        raise RuntimeError('separate process required')
    sys.path.insert(0, str(_ROOT))
    import io
    import tempfile
    from contextlib import redirect_stdout
    from urllib.parse import quote, quote_plus
    from types import SimpleNamespace
    audit = {'network_attempts': 0, 'credential_file_reads': 0}

    def guard(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.gethostbyname'):
            audit['network_attempts'] += 1
            raise RuntimeError('OFFLINE_NETWORK_FORBIDDEN')
        if event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(args[0])).name
            if name == '.env' or name.startswith('.env.') and name != '.env.example':
                audit['credential_file_reads'] += 1
                raise RuntimeError('OFFLINE_CREDENTIAL_FILE_FORBIDDEN')
    sys.addaudithook(guard)
    from app.agents import tools as t
    from app.agents.schemas import AgentRole, RunContext
    import httpx
    no_kan_import = not any(name.startswith('app.features.supplier_crawl') for name in sys.modules)
    from app.features.supplier_crawl.application.trial import SupplierCrawlTrial as RegistrationTrial
    from app.features.supplier_crawl.application import tool as registration_binding
    from scripts import crawl_trial as cli

    def settings(**values):
        return t.CrawlSettings(_env_file=None, crawl_delay_seconds=0.5, **values)

    def data_rows():
        return [dict(LCNS_NO='LIC-1', BSSH_NM='테스트식품', PRDLST_REPORT_NO=report,
                     PRDLST_NM='냉동딸기', PRDLST_DCNM='과·채가공품', PRMS_DT='20260101', PRODUCTION='N')
                for report in ('REPORT-1', 'REPORT-2')]

    def route(request):
        url=str(request.url)
        if '/I1250/' in url:
            return httpx.Response(200,json={'I1250':{'RESULT':{'CODE':'INFO-000'},'row':data_rows()}})
        if '/I1220/' in url:
            return httpx.Response(200,json={'I1220':{'RESULT':{'CODE':'INFO-000'},'row':[
                dict(LCNS_NO='LIC-1',BSSH_NM='테스트식품',TELNO='02-123-4567',LOCP_ADDR='서울 강남구',INDUTY_NM='식품')]}})
        if 'apis.data.go.kr' in url:
            return httpx.Response(200,json={'response':{'header':{'resultCode':'OK'},'body':{'totalCount':1,'items':[
                {'item':dict(licenseno='LIC-1',appointno='CERT-1',company='테스트식품',businessnm='식품',species='과·채가공품',
                             sido='서울',sgg='강남구',issuedate='20260101',issueenddate='20271231')}]}}})
        raise AssertionError('Unexpected offline route: '+request.url.host)

    def collect(cls=t.SupplierCrawlTrial):
        cfg=settings(food_safety_api_key='SYNTHETIC',data_go_kr_service_key='SYNTHETIC-HACCP')
        with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
            fetcher=t.Fetcher(cfg,transport=httpx.MockTransport(route),sleep=lambda _:None)
            try:
                return cls(cfg,fetcher).run(t.TrialOptions(keyword='딸기',sources=('public','haccp')))
            finally:
                fetcher.close()

    class ToolBoundary(unittest.TestCase):
        def test_tool_is_defined_but_never_role_registered(self):
            self.assertIsInstance(t.crawl_supplier_candidates,t.BaseTool)
            for role in AgentRole:
                self.assertNotIn('crawl_supplier_candidates',t.ROLE_TOOLS[role])
                self.assertNotIn('crawl_supplier_candidates',[x.name for x in t.tools_for(role)])
            self.assertNotIn('crawl_supplier_candidates',t.KNOWN_TOOLS)
            self.assertNotIn('crawl_supplier_candidates',t._registry)

        def test_registration_rejects_collection_tool_without_registry_mutation(self):
            before=dict(t._registry)
            with self.assertRaisesRegex(ValueError,'not in ROLE_TOOLS'):
                t.register(t.crawl_supplier_candidates)
            self.assertEqual(t._registry,before)

        def test_existing_duplicate_guard_is_preserved(self):
            with self.assertRaisesRegex(ValueError,'already registered'):
                t.register(t.get_case_context)

        def test_shared_collection_import_does_not_load_registration(self):
            self.assertTrue(no_kan_import)
            self.assertFalse(hasattr(registration_binding,'install_crawl_tool'))
            self.assertFalse(hasattr(registration_binding,'crawl_supplier_candidates'))

        def test_tool_schema_exposes_collection_options_only(self):
            schema=t.crawl_supplier_candidates.get_input_schema().model_json_schema()
            self.assertEqual(set(schema['properties']),{'options'})
            self.assertIn('keyword',schema['$defs']['TrialOptions']['properties'])
            for name in ('owner_id','case_id','input_revision','supplier_id','p_items'):
                self.assertNotIn(name,json.dumps(schema))

        def test_tool_cannot_bypass_existing_cli_source_and_count_bounds(self):
            from pydantic import ValidationError
            for values in ({'sources':['other']},{'sources':[]},{'item_limit':1001},{'business_limit':51},
                           {'haccp_pages':201},{'haccp_rows':1001},{'site_limit':51},{'item_limit':0}):
                with self.subTest(values=values),mock.patch.object(t,'CrawlSettings') as configure:
                    with self.assertRaises(ValidationError):
                        t.crawl_supplier_candidates.invoke({'options':{'keyword':'딸기',**values}})
                    configure.assert_not_called()

        def test_tool_invocation_returns_collection_only(self):
            cfg=settings(food_safety_api_key='SYNTHETIC')
            real_fetcher=t.Fetcher
            with mock.patch.object(t,'CrawlSettings',return_value=cfg), mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']), mock.patch.object(t,'Fetcher',side_effect=lambda s:real_fetcher(s,transport=httpx.MockTransport(route),sleep=lambda _:None)):
                result=t.crawl_supplier_candidates.invoke({'options':{'keyword':'딸기','sources':['public']}})
            self.assertEqual(len(result['candidates']),1)
            self.assertIsNone(result['candidates'][0]['verified_supplier_id'])
            self.assertNotIn('registration',result)
            self.assertNotIn('evaluation_item_ids',result)
            self.assertNotIn('evidence',result['candidates'][0])

        def test_registration_binding_stays_explicit_and_resets(self):
            context=RunContext('case',1,'run')
            self.assertEqual(registration_binding.run_crawl_registration(context),{'status':'NOT_CONFIGURED'})
            service=mock.Mock();service.run.return_value={'status':'SYNTHETIC'}
            with registration_binding.bind_crawl_service(service):
                self.assertEqual(registration_binding.run_crawl_registration(context),{'status':'SYNTHETIC'})
            self.assertEqual(registration_binding.run_crawl_registration(context),{'status':'NOT_CONFIGURED'})
            service.run.assert_called_once_with(context)

    class CollectionAndCLI(unittest.TestCase):
        def test_three_source_config_and_unconfirmed_candidate(self):
            report=collect()
            self.assertEqual([s.source_type for s in report.sources],['PUBLIC_DATA','HACCP','HOMEPAGE'])
            candidate=report.candidates[0]
            self.assertEqual(candidate.checklist_status,'UNCONFIRMED')
            self.assertIsNone(candidate.verified_supplier_id)
            self.assertEqual(candidate.location,'서울 강남구')
            self.assertEqual(candidate.certifications[0].certification_no,'CERT-1')
            self.assertEqual(len(candidate.handled_items),1)

        def test_registration_extension_preserves_report_numbers_and_provenance(self):
            report=collect(RegistrationTrial)
            candidate=report.candidates[0]
            self.assertEqual([x.report_no for x in candidate.handled_items],['REPORT-1','REPORT-2'])
            self.assertEqual(candidate.certifications[0].license_no,'LIC-1')
            self.assertEqual(set(candidate.evidence),{'name','license_no','location'})
            self.assertEqual(candidate.evidence['name'].record_ref,'REPORT-1')
            serialized=report.model_dump(mode='json')['candidates'][0]
            self.assertEqual(serialized['handled_items'][1]['report_no'],'REPORT-2')
            self.assertEqual(serialized['certifications'][0]['license_no'],'LIC-1')

        def test_homepage_registration_extension_keeps_missing_report_explicit(self):
            cfg=settings()
            def handler(request):
                if request.url.path == '/robots.txt':
                    return httpx.Response(200,text='User-agent: *\nAllow: /')
                return httpx.Response(200,headers={'content-type':'text/html; charset=utf-8'},text='<title>테스트식품</title><p>딸기 test@public.example</p>')
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
                fetcher=t.Fetcher(cfg,transport=httpx.MockTransport(handler),sleep=lambda _:None)
                try:
                    report=RegistrationTrial(cfg,fetcher).run(t.TrialOptions(keyword='딸기',sources=('homepage',),seeds=(t.SiteSeed('https://public.example/',name='테스트식품'),)))
                finally:fetcher.close()
            self.assertEqual(len(report.candidates),1)
            item=report.candidates[0].handled_items[0]
            self.assertIsNone(item.report_no)
            self.assertIsNone(report.model_dump(mode='json')['candidates'][0]['handled_items'][0]['report_no'])

        def test_cli_uses_moved_collection_and_writes_masked_report(self):
            self.assertIs(cli.SupplierCrawlTrial,t.SupplierCrawlTrial)
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--keyword','딸기','--sources','public','--out',directory],settings=settings(food_safety_api_key='SYNTHETIC'),transport=httpx.MockTransport(route)),0)
                outputs=list(Path(directory).iterdir())
                self.assertEqual({p.suffix for p in outputs},{'.json','.md'})
                self.assertTrue(all('SYNTHETIC' not in p.read_text(encoding='utf-8') for p in outputs))

        def test_cli_limits_still_reject_invalid_requests(self):
            with mock.patch('sys.stderr',new=io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_args(['--keyword','딸기','--site-limit','51'])

        def test_item_filters_and_ended_items_remain_excluded(self):
            cfg=settings(food_safety_api_key='SYNTHETIC')
            rows=data_rows();rows[0]['PRODUCTION']='Y';rows[1]['PRDLST_DCNM']='기타'
            def handler(request):
                if '/I1250/' in str(request.url):
                    return httpx.Response(200,json={'I1250':{'RESULT':{'CODE':'INFO-000'},'row':rows}})
                raise AssertionError('No business lookup for excluded items')
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
                fetcher=t.Fetcher(cfg,transport=httpx.MockTransport(handler),sleep=lambda _:None)
                try:
                    report=t.SupplierCrawlTrial(cfg,fetcher).run(t.TrialOptions(keyword='딸기',sources=('public',),item_types=('과채가공품',)))
                finally:fetcher.close()
            self.assertEqual(report.candidates,[])
            self.assertIn('ITEM_FILTERED',[x.code for x in report.issues])

    class Security(unittest.TestCase):
        def fetch(self,url,handler,**kwargs):
            fetcher=t.Fetcher(settings(),transport=httpx.MockTransport(handler),sleep=lambda _:None)
            try:return fetcher.get(url,**kwargs)
            finally:fetcher.close()

        def test_private_and_disguised_literals_are_rejected(self):
            for url in ('http://127.0.0.1/','http://10.1.1.1/','http://[::1]/','http://localhost/','http://user:pass@public.example/','ftp://public.example/','http://[::ffff:127.0.0.1]/'):
                with self.subTest(url=url):self.assertFalse(t.is_public_web_url(url))

        def test_mixed_dns_rejects_entire_answer(self):
            answers=[(t.socket.AF_INET,1,6,'',('93.184.216.34',443)),(t.socket.AF_INET,1,6,'',('127.0.0.1',443))]
            with mock.patch.object(t.socket,'getaddrinfo',return_value=answers),self.assertRaises(t.UnsafeAddress):
                t.resolve_public('public.example',443)

        def test_backend_connects_only_validated_numeric_address(self):
            sock=mock.Mock();sock.getpeername.return_value=('93.184.216.34',443)
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']),mock.patch.object(t.socket,'socket',return_value=sock),mock.patch.object(t.socket,'create_connection',side_effect=AssertionError('No hostname reconnect')):
                stream=t._PublicBackend().connect_tcp('public.example',443)
            sock.connect.assert_called_once_with(('93.184.216.34',443))
            stream.close()

        def test_backend_rejects_peer_mismatch(self):
            sock=mock.Mock();sock.getpeername.return_value=('127.0.0.1',443)
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']),mock.patch.object(t.socket,'socket',return_value=sock),self.assertRaises(t.UnsafeAddress):
                t._PublicBackend().connect_tcp('public.example',443)
            sock.close.assert_called_once()

        def test_credential_redirect_chain_cannot_escape_initial_origin(self):
            calls=[]
            def handler(request):
                calls.append(str(request.url))
                return httpx.Response(302,headers={'Location':'/next' if len(calls)==1 else 'https://other.example/'})
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']),self.assertRaises(t.FetchError) as caught:
                self.fetch('https://public.example/?serviceKey=SYNTHETIC',handler)
            self.assertEqual(caught.exception.code,'AUTH_REDIRECT_BLOCKED')
            self.assertEqual(len(calls),2)

        def test_header_credentials_cannot_cross_origin(self):
            handler=mock.Mock(return_value=httpx.Response(302,headers={'Location':'https://other.example/'}))
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']),self.assertRaises(t.FetchError) as caught:
                self.fetch('https://public.example/',handler,headers={'Authorization':'SYNTHETIC'})
            self.assertEqual(caught.exception.code,'AUTH_REDIRECT_BLOCKED')
            self.assertEqual(handler.call_count,1)

        def test_redirect_destination_is_validated_before_io(self):
            handler=mock.Mock(return_value=httpx.Response(302,headers={'Location':'http://127.0.0.1/'}))
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']),self.assertRaises(t.FetchError) as caught:
                self.fetch('https://public.example/',handler)
            self.assertEqual(caught.exception.code,'URL_NOT_ALLOWED');self.assertEqual(handler.call_count,1)

        def test_response_size_limit_is_preserved(self):
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']),self.assertRaises(t.FetchError) as caught:
                self.fetch('https://public.example/',lambda _:httpx.Response(200,content=b'x'*(t.MAX_RESPONSE_BYTES+1)))
            self.assertEqual(caught.exception.code,'RESPONSE_TOO_LARGE')

        def test_percent_and_json_encoded_secrets_are_masked(self):
            secret='SYNTHETIC+/한글'
            for value in (secret,quote(secret,safe=''),quote_plus(secret,safe=''),json.dumps(secret,ensure_ascii=True)[1:-1]):
                with self.subTest(value=value):self.assertNotIn(value,t.mask_url('prefix '+value,[secret]))
            self.assertEqual(t.mask_url('https://public.example/?ServiceKey=anything',[]),'https://public.example/?ServiceKey=***')

        def test_environment_proxy_is_not_trusted(self):
            real=httpx.Client
            with mock.patch.object(t.httpx,'Client',wraps=real) as client:
                fetcher=t.Fetcher(settings(),transport=httpx.MockTransport(lambda _:httpx.Response(200)))
                fetcher.close()
            self.assertIs(client.call_args.kwargs['trust_env'],False)
            self.assertIs(client.call_args.kwargs['follow_redirects'],False)

        def test_robots_disallow_never_fetches_private_page(self):
            paths=[]
            def handler(request):
                paths.append(request.url.path)
                return httpx.Response(200,text='User-agent: *\nDisallow: /private')
            cfg=settings()
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
                fetcher=t.Fetcher(cfg,transport=httpx.MockTransport(handler))
                try:result=t.HomepageCrawler(fetcher,cfg).crawl(t.SiteSeed('https://public.example/private'),'딸기')
                finally:fetcher.close()
            self.assertEqual(paths,['/robots.txt']);self.assertEqual(result.pages,[])
            self.assertEqual(result.blocked_by_robots,['https://public.example/private'])

        def test_robots_failure_remains_closed(self):
            paths=[]
            def handler(request):paths.append(request.url.path);return httpx.Response(503)
            cfg=settings()
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
                fetcher=t.Fetcher(cfg,transport=httpx.MockTransport(handler))
                try:result=t.HomepageCrawler(fetcher,cfg).crawl(t.SiteSeed('https://public.example/'),'딸기')
                finally:fetcher.close()
            self.assertEqual(paths,['/robots.txt']);self.assertEqual(result.errors[0][0],'ROBOTS_UNAVAILABLE')

        def test_homepage_redirect_scope_is_preserved(self):
            paths=[]
            def handler(request):
                paths.append(request.url.path)
                if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nAllow: /')
                return httpx.Response(302,headers={'Location':'https://other.example/'})
            cfg=settings()
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
                fetcher=t.Fetcher(cfg,transport=httpx.MockTransport(handler),sleep=lambda _:None)
                try:result=t.HomepageCrawler(fetcher,cfg).crawl(t.SiteSeed('https://public.example/'),'딸기')
                finally:fetcher.close()
            self.assertEqual(paths,['/robots.txt','/']);self.assertEqual(result.errors[0][0],'REDIRECT_NOT_ALLOWED')

        def test_platform_directory_and_placeholder_contacts_stay_excluded(self):
            self.assertTrue(t.is_platform_url('https://youtube.com/company'))
            self.assertTrue(t.is_directory_url('https://saramin.co.kr/company'))
            self.assertEqual(t.clean_emails(['hello@example.com','real@public.example','real@public.example']),['real@public.example'])

        def test_delay_and_cleanup_are_preserved(self):
            delay=mock.Mock();transport=httpx.MockTransport(lambda _:httpx.Response(200))
            fetcher=t.Fetcher(settings(),transport=transport,sleep=delay,clock=lambda:10)
            with mock.patch.object(t,'resolve_public',return_value=['93.184.216.34']):
                fetcher.get('https://public.example/');fetcher.get('https://public.example/next')
            delay.assert_called_once_with(0.5)
            fetcher.close();self.assertTrue(fetcher._client.is_closed)

    suite=unittest.TestSuite(unittest.TestLoader().loadTestsFromTestCase(c) for c in (ToolBoundary,CollectionAndCLI,Security))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    summary={'pid':os.getpid(),'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'audit':audit}
    print(_TAG+json.dumps(summary,sort_keys=True))
    return 0 if result.wasSuccessful() and not any(audit.values()) else 1


class IsolatedCrawlRelocationTests(unittest.TestCase):
    def test_import_is_inert(self):
        before=dict(os.environ);path=list(sys.path)
        spec=importlib.util.spec_from_file_location('crawl_move_probe',__file__)
        with mock.patch.object(sys,'addaudithook') as hook,mock.patch.object(subprocess,'run') as spawn:
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        self.assertTrue(dict(os.environ)==before);self.assertEqual(sys.path,path)
        hook.assert_not_called();spawn.assert_not_called()

    def test_relocation_regressions_in_child(self):
        before=dict(os.environ)
        prefixes=('OPENAI_','ANTHROPIC_','LLM_','SUPABASE_','GMAIL_','NAVER_','FOOD_SAFETY_','DATA_GO_','LANGSMITH_','LANGCHAIN_','RUN_LIVE')
        environment={k:v for k,v in os.environ.items() if not k.startswith(prefixes) and k.upper() not in {'PYTHONPATH','PYTHONHOME','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'}}
        result=subprocess.run([sys.executable,'-I','-B','-X','utf8',str(Path(__file__).resolve()),_FLAG],cwd=_ROOT,env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',timeout=60)
        self.assertTrue(dict(os.environ)==before)
        self.assertEqual(result.returncode,0,result.stdout)
        records=[json.loads(x[len(_TAG):]) for x in result.stdout.splitlines() if x.startswith(_TAG)]
        self.assertEqual(len(records),1,result.stdout)
        self.assertNotEqual(records[0]['pid'],os.getpid())
        self.assertEqual((records[0]['failures'],records[0]['errors'],records[0]['skipped']),(0,0,0))
        self.assertEqual(records[0]['audit'],{'network_attempts':0,'credential_file_reads':0})
        print(result.stdout,end='')


if __name__=='__main__':
    if sys.argv[1:]==[_FLAG]:raise SystemExit(_worker())
    unittest.main()

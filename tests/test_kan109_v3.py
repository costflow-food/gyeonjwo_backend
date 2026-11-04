"""Import-safe KAN-109 regression launcher; service tests run only in a child."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

_RESULT_TAG = "KAN109_V3_WORKER_RESULT="
_WORKER_FLAG = "--kan109-worker"
_ROOT = Path(__file__).resolve().parents[1]
_SECRET_PREFIXES = ("OPENAI_", "ANTHROPIC_", "LLM_", "SUPABASE_", "GMAIL_", "NAVER_",
                    "FOOD_SAFETY_", "DATA_GO_", "LANGSMITH_", "LANGCHAIN_", "RUN_LIVE")


def _run_in_subprocess():
    # Copy/filter only the child environment; importing/discovering this module
    # does not modify os.environ, sys.path, audit hooks or service registries.
    excluded = {"PYTHONPATH", "PYTHONHOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(_SECRET_PREFIXES) and key.upper() not in excluded}
    return subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), _WORKER_FLAG],
                          cwd=_ROOT, env=environment, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", timeout=60)


def _run_worker():
    if __name__ != "__main__" or sys.argv[1:] != [_WORKER_FLAG]:
        raise RuntimeError("KAN-109 worker must be launched as a separate process")
    # -I excludes cwd/PYTHONPATH/user site; add only this repository in the child.
    sys.path.insert(0, str(_ROOT))
    from copy import deepcopy
    from datetime import datetime, timezone
    import inspect
    import json
    import os
    from pathlib import Path
    from types import SimpleNamespace
    import unittest
    from uuid import UUID

    AUDIT = {"network_attempts": 0, "credential_file_reads": 0}
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "ANTHROPIC_", "LLM_", "SUPABASE_", "GMAIL_", "NAVER_", "FOOD_SAFETY_", "DATA_GO_")):
            os.environ.pop(key, None)

    def offline_audit(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.gethostbyname"):
            AUDIT["network_attempts"] += 1
            raise RuntimeError("OFFLINE_NETWORK_FORBIDDEN")
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(args[0])).name
            if name == ".env" or name.startswith(".env.") and name != ".env.example":
                AUDIT["credential_file_reads"] += 1
                raise RuntimeError("OFFLINE_CREDENTIAL_FILE_FORBIDDEN")
    sys.addaudithook(offline_audit)

    from app.agents.schemas import RunContext
    from app.features.supplier_crawl.application.candidates import CrawlSupplierCandidates
    from app.features.supplier_crawl.domain.registration import PayloadBuilder, RegistrationError, RegistrationPayload
    from app.features.supplier_crawl.domain.schemas import CrawledSupplierCandidate, SourceRef, HandledItem, Contact
    from app.features.supplier_crawl.infrastructure.registration import WebSupplierRegistrar, RegistrationFailure, validate_result

    NOW = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)
    def uid(n): return str(UUID(int=n))
    OWNER, CASE, RUN, SUPPLIER = uid(1), uid(2), uid(3), uid(10)
    ITEM1, ITEM2, CANDIDATE = uid(11), uid(12), uid(20)
    CONTEXT = RunContext(CASE, 7, RUN)
    KEYS = ("legacy_frozen_strawberry", "item_" + "a" * 64)

    def source(kind="PUBLIC_DATA", record=None):
        return SourceRef(source_type=kind, source_url="https://example.invalid/catalog", fetched_at=NOW, record_ref=record)

    def candidate(*, email=True, reports=("1001",), name="?? ??"):
        ref = source(record="99911040003")
        return CrawledSupplierCandidate(candidate_key="SYNTHETIC", name="SYNTHETIC ??", license_no="99911040003",
            evidence={"name": ref, "license_no": ref},
            contacts=[Contact(contact_type="EMAIL", value="buyer@example.invalid", evidence=source("HOMEPAGE"))] if email else [],
            handled_items=[HandledItem(item_name=name, item_type="????", report_no=r, evidence=source(record=r)) for r in reports])

    def old_compatible_payload(*, email=True, reports=("1001",)):
        supplier={"name":"SYNTHETIC ??", "license_no":"99911040003"}
        if email: supplier["contact_email"]="buyer@example.invalid"
        return RegistrationPayload(supplier, [{"report_no":r,"item_name":"?? ??","item_type":"????","evidence":{}} for r in reports])

    def response(action="CREATED", reports=("1001",), keys=KEYS):
        if action == "CONTACT_REQUIRED":
            return {"action":action, "candidate_id":CANDIDATE,"supplier_id":None,"supplier_item_ids":[],"item_mappings":[],"rfq_eligible":False}
        if action == "LINKED_REGISTERED":
            return {"action":action, "supplier_id":SUPPLIER,"supplier_item_ids":[],"item_mappings":[],"requires_item_mapping":True}
        ids=[ITEM1, ITEM2][:len(reports)]
        return {"action":action, "supplier_id":SUPPLIER,"supplier_item_ids":ids,
                "item_mappings":[{"report_no":r,"supplier_item_id":i,"standardized_item_key":k} for r,i,k in zip(reports,ids,keys)],
                "requires_item_mapping":False}

    class RPCError(Exception):
        def __init__(self, code, message):
            self.code, self.message = code, message
            super().__init__("SYNTHETIC provider error: do not expose")
    class Client:
        def __init__(self, replies):
            self.replies=list(replies);self.calls=[]
        def rpc(self,name,params):
            self.calls.append((name,deepcopy(params)))
            def execute():
                result=self.replies.pop(0)
                if isinstance(result,Exception): raise result
                return SimpleNamespace(data=deepcopy(result))
            return SimpleNamespace(execute=execute)
        def table(self,*args): raise AssertionError("No direct table writes or fallback")

    class Store:
        def __init__(self):
            self.role_runs={RUN:"CANDIDATE"};self.reads=[]
            self.supplier={"id":SUPPLIER,"source_type":"VERIFIED_WEB","contact_email":"buyer@example.invalid"}
            # Existing GraphStore does not expose standardized_item_key in ITEM_COLUMNS.
            self.rows=[{"id":ITEM1,"supplier_id":SUPPLIER,"certifications_jsonb":None},
                       {"id":ITEM2,"supplier_id":SUPPLIER,"certifications_jsonb":None}]
            self.case={"input_revision":7,"item_name":"?? ??","mandatory_conditions":[]}
        def get_candidate_conditions(self,case):
            assert case==CASE
            return deepcopy(self.case)
        def get_supplier(self,case,supplier):
            assert case==CASE
            self.reads.append(("supplier",supplier));return deepcopy(self.supplier)
        def get_supplier_items(self,case,ids):
            assert case==CASE
            self.reads.append(("items",list(ids)));return deepcopy([r for r in self.rows if r["id"] in ids])

    class Scope:
        def __init__(self,replies):
            self.owner=OWNER;self.case_id=CASE;self.revision=7;self.agent_store=Store()
            self.client=Client(replies);self.repo=SimpleNamespace(client=self.client);self.checked=[]
        def check(self,case,revision):
            self.checked.append((case,revision))
            if case!=self.case_id: raise RegistrationError("RESOURCE_NOT_FOUND")
            if revision!=self.revision: raise RegistrationError("REVISION_CONFLICT")

    def service(scope, candidates):
        report=SimpleNamespace(candidates=candidates,issues=[])
        return CrawlSupplierCandidates(collect=lambda options:report,
            builder=PayloadBuilder(clock=lambda:NOW),registrar=WebSupplierRegistrar(scope),store=scope.agent_store)

    class PayloadTests(unittest.TestCase):
        def test_default_payload_omits_key_and_keeps_required_evidence(self):
            payload=PayloadBuilder(clock=lambda:NOW).build(candidate())
            self.assertNotIn("standardized_item_key",payload.items[0])
            self.assertEqual(payload.items[0]["report_no"],"1001")
            self.assertEqual(payload.items[0]["evidence"]["record_ref"],"1001")
            self.assertEqual(payload.items[0]["item_name"],"?? ??")
        def test_contact_candidate_also_needs_no_resolver(self):
            payload=PayloadBuilder(clock=lambda:NOW).build(candidate(email=False))
            self.assertNotIn("contact_email",payload.supplier)
            self.assertNotIn("standardized_item_key",payload.items[0])
        def test_names_are_not_normalized_into_local_keys(self):
            value="  Changed   Name "
            payload=PayloadBuilder(clock=lambda:NOW).build(candidate(name=value))
            self.assertEqual(payload.items[0]["item_name"],value)
            self.assertNotIn("standardized_item_key",payload.items[0])
        def test_missing_report_still_blocks_before_rpc(self):
            value=candidate();value.handled_items[0].report_no=None
            with self.assertRaisesRegex(RegistrationError,"REPORT_MAPPING_REQUIRED"):
                PayloadBuilder(clock=lambda:NOW).build(value)
        def test_missing_field_evidence_still_blocks(self):
            value=candidate();value.evidence.pop("license_no")
            with self.assertRaisesRegex(RegistrationError,"MISSING_FIELD_EVIDENCE"):
                PayloadBuilder(clock=lambda:NOW).build(value)
        def test_duplicate_report_still_blocks(self):
            with self.assertRaisesRegex(RegistrationError,"DUPLICATE_WEB_REPORT"):
                PayloadBuilder(clock=lambda:NOW).build(candidate(reports=("1001","1001")))
        def test_composition_has_no_required_local_key_producer(self):
            from app.features.supplier_crawl.infrastructure.composition import make_crawl_service
            scope=Scope([])
            settings=SimpleNamespace(secrets=lambda:[])
            self.assertNotIn("key_resolver",inspect.signature(make_crawl_service).parameters)
            made=make_crawl_service(scope=scope,settings=settings)
            self.assertIsInstance(made,CrawlSupplierCandidates)
            self.assertEqual(scope.client.calls,[])

    class ResponseTests(unittest.TestCase):
        def valid(self,value=None,payload=None):
            return validate_result(value or response(),payload or old_compatible_payload())
        def invalid(self,value,payload=None):
            with self.assertRaises(RegistrationFailure) as caught:self.valid(value,payload)
            self.assertEqual(caught.exception.code,"INVALID_WEB_RPC_RESULT")
            self.assertTrue(caught.exception.outcome_unknown)
        def test_created_accepts_actual_db_key(self):
            value=response();self.assertEqual(self.valid(value),value)
        def test_updated_keeps_legacy_key_after_name_change(self):
            p=old_compatible_payload();p.items[0]["item_name"]="Unseen changed name"
            value=response("UPDATED",keys=("old_preserved_key",))
            self.assertEqual(self.valid(value,p)["item_mappings"][0]["standardized_item_key"],"old_preserved_key")
        def test_multiple_reports_and_mapping_order_are_correlated(self):
            p=old_compatible_payload(reports=("1001","1002"));value=response(reports=("1001","1002"))
            value["item_mappings"].reverse()
            self.assertEqual(self.valid(value,p),value)
        def test_missing_returned_key_is_not_old_contract_success(self):
            value=response();value["item_mappings"][0].pop("standardized_item_key");self.invalid(value)
        def test_null_key_is_invalid(self):
            value=response();value["item_mappings"][0]["standardized_item_key"]=None;self.invalid(value)
        def test_blank_key_is_invalid(self):
            value=response();value["item_mappings"][0]["standardized_item_key"]=" \t";self.invalid(value)
        def test_non_string_key_is_invalid(self):
            value=response();value["item_mappings"][0]["standardized_item_key"]=42;self.invalid(value)
        def test_foreign_report_is_invalid(self):
            value=response();value["item_mappings"][0]["report_no"]="9000";self.invalid(value)
        def test_duplicate_report_is_invalid(self):
            p=old_compatible_payload(reports=("1001","1002"));value=response(reports=("1001","1002"))
            value["item_mappings"][1]["report_no"]="1001";self.invalid(value,p)
        def test_missing_mapping_is_invalid(self):
            value=response();value["item_mappings"]=[];self.invalid(value)
        def test_mapping_id_must_match_returned_actual_ids(self):
            value=response();value["item_mappings"][0]["supplier_item_id"]=ITEM2;self.invalid(value)
        def test_invalid_uuid_is_not_accepted(self):
            value=response();value["supplier_id"]="invented";self.invalid(value)
        def test_duplicate_actual_ids_are_invalid(self):
            p=old_compatible_payload(reports=("1001","1002"));value=response(reports=("1001","1002"))
            value["supplier_item_ids"]=[ITEM1,ITEM1];self.invalid(value,p)
        def test_optional_caller_key_must_match_returned_key(self):
            p=old_compatible_payload();p.items[0]["standardized_item_key"]="wrong"
            self.invalid(response(),p)
        def test_optional_matching_caller_key_remains_compatible(self):
            p=old_compatible_payload();p.items[0]["standardized_item_key"]=KEYS[0]
            self.assertEqual(self.valid(response(),p)["item_mappings"][0]["standardized_item_key"],KEYS[0])
        def test_linked_registered_preserves_empty_mapping(self):
            value=response("LINKED_REGISTERED");self.assertEqual(self.valid(value),value)
        def test_linked_registered_cannot_claim_mapping_complete(self):
            value=response("LINKED_REGISTERED");value["requires_item_mapping"]=False;self.invalid(value)
        def test_contact_required_has_no_supplier_or_item_mapping(self):
            value=response("CONTACT_REQUIRED")
            self.assertEqual(self.valid(value,old_compatible_payload(email=False)),value)
        def test_contact_required_cannot_be_rfq_eligible(self):
            value=response("CONTACT_REQUIRED");value["rfq_eligible"]=True
            self.invalid(value,old_compatible_payload(email=False))

    class PipelineTests(unittest.TestCase):
        def test_created_uses_real_ids_keys_and_existing_graph_store(self):
            scope=Scope([response()]);out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["evaluation_item_ids"],[ITEM1])
            self.assertEqual(out["results"][0]["registration"]["item_mappings"][0]["standardized_item_key"],KEYS[0])
            name,params=scope.client.calls[0]
            self.assertEqual(name,"upsert_web_supplier")
            self.assertEqual(set(params),{"p_owner","p_case","p_expected_revision","p_supplier","p_items"})
            self.assertEqual((params["p_owner"],params["p_case"],params["p_expected_revision"]),(OWNER,CASE,7))
            self.assertNotIn("standardized_item_key",params["p_items"][0])
            self.assertIn(("items",[ITEM1]),scope.agent_store.reads)
        def test_contact_required_is_preserved_without_supplier_reads(self):
            scope=Scope([response("CONTACT_REQUIRED")]);out=service(scope,[candidate(email=False)]).run(CONTEXT)
            self.assertEqual(out["results"][0]["registration"]["candidate_id"],CANDIDATE)
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertEqual(scope.agent_store.reads,[])
        def test_linked_registered_never_adds_or_queries_items(self):
            scope=Scope([response("LINKED_REGISTERED")]);scope.agent_store.supplier["source_type"]="REGISTERED"
            out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["results"][0]["registration"]["supplier_id"],SUPPLIER)
            self.assertIn("REGISTERED_ITEM_MAPPING_REQUIRED",out["results"][0]["blockers"])
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertEqual(scope.agent_store.reads,[("supplier",SUPPLIER)])
        def test_object_certificates_block_both_lists_and_keep_real_registration(self):
            scope=Scope([response()]);scope.agent_store.rows[0]["certifications_jsonb"]=[{"type":"HACCP"}]
            out=service(scope,[candidate()]).run(CONTEXT);item=out["results"][0]
            self.assertEqual(item["registration"],response())
            self.assertEqual(item["evaluation_item_ids"],[])
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertIn("TEAM_CERTIFICATION_FORMAT_REQUIRED",item["blockers"])
        def test_unknown_empty_certificates_are_not_confirmed_failures(self):
            scope=Scope([response()]);scope.agent_store.case["mandatory_conditions"]={"certifications":["HACCP"]}
            scope.agent_store.rows[0]["certifications_jsonb"]=[]
            out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertIn("TEAM_WEB_CERTIFICATION_UNKNOWN_REQUIRED",out["results"][0]["blockers"])
        def test_readback_failure_keeps_registration_but_excludes_ids(self):
            scope=Scope([response()]);scope.agent_store.rows=[]
            out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["results"][0]["registration"],response())
            self.assertEqual(out["results"][0]["evaluation_item_ids"],[])
            self.assertEqual(out["evaluation_item_ids"],[])
        def test_optional_key_readback_mismatch_is_blocked(self):
            scope=Scope([response()]);scope.agent_store.rows[0]["standardized_item_key"]="different"
            out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertEqual(out["results"][0]["registration"],response())
        def test_item_key_mismatch_fails_whole_multi_item_rpc_once(self):
            scope=Scope([RPCError("22023","ITEM_KEY_MISMATCH")])
            out=service(scope,[candidate(reports=("1001","1002"))]).run(CONTEXT)
            self.assertEqual(len(scope.client.calls),1)
            self.assertEqual(len(scope.client.calls[0][1]["p_items"]),2)
            self.assertEqual(out["results"][0]["code"],"ITEM_KEY_MISMATCH")
            self.assertIsNone(out["results"][0]["registration"])
            self.assertFalse(out["results"][0]["outcome_unknown"])
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertEqual(scope.agent_store.reads,[])
        def test_unknown_rpc_failure_stops_and_never_retries(self):
            scope=Scope([RPCError("08006","SYNTHETIC secret provider message"),response()])
            out=service(scope,[candidate(),candidate()]).run(CONTEXT)
            self.assertEqual(len(scope.client.calls),1)
            self.assertTrue(out["results"][0]["outcome_unknown"])
            self.assertNotIn("secret",json.dumps(out))
            self.assertEqual(out["evaluation_item_ids"],[])
        def test_malformed_success_remains_outcome_unknown(self):
            value=response();value["item_mappings"][0].pop("standardized_item_key")
            scope=Scope([value,response()]);out=service(scope,[candidate(),candidate()]).run(CONTEXT)
            self.assertEqual(len(scope.client.calls),1)
            self.assertTrue(out["results"][0]["outcome_unknown"])
            self.assertIsNone(out["results"][0]["registration"])
            self.assertEqual(out["evaluation_item_ids"],[])
        def test_stale_revision_blocks_before_collection_and_rpc(self):
            scope=Scope([response()]);scope.revision=8
            with self.assertRaisesRegex(RegistrationError,"REVISION_CONFLICT"): service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(scope.client.calls,[])
        def test_wrong_case_is_not_an_rpc_argument(self):
            scope=Scope([response()])
            with self.assertRaisesRegex(RegistrationError,"RESOURCE_NOT_FOUND"):
                service(scope,[candidate()]).run(RunContext(uid(99),7,RUN))
            self.assertEqual(scope.client.calls,[])
        def test_wrong_role_blocks_before_rpc(self):
            scope=Scope([response()]);scope.agent_store.role_runs[RUN]="INFO_EXTRACTOR"
            with self.assertRaisesRegex(RegistrationError,"CRAWL_SCOPE_NOT_ALLOWED"): service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(scope.client.calls,[])
        def test_ambiguous_key_error_is_safe_failure(self):
            scope=Scope([RPCError("22023","AMBIGUOUS_ITEM_KEY")])
            out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["results"][0]["code"],"AMBIGUOUS_ITEM_KEY")
            self.assertEqual(out["evaluation_item_ids"],[])
        def test_contact_missing_after_registration_keeps_ids_but_excludes_evaluation(self):
            scope=Scope([response()]);scope.agent_store.supplier["contact_email"]=None
            out=service(scope,[candidate()]).run(CONTEXT);item=out["results"][0]
            self.assertEqual(item["registration"],response())
            self.assertEqual(item["evaluation_item_ids"],[])
            self.assertEqual(out["evaluation_item_ids"],[])
            self.assertIn("REGISTERED_CONTACT_REQUIRED",item["blockers"])
        def test_foreign_supplier_item_readback_is_blocked(self):
            scope=Scope([response()]);scope.agent_store.rows[0]["supplier_id"]=uid(99)
            out=service(scope,[candidate()]).run(CONTEXT)
            self.assertEqual(out["results"][0]["registration"],response())
            self.assertEqual(out["results"][0]["evaluation_item_ids"],[])
            self.assertEqual(out["evaluation_item_ids"],[])
        def _mixed(self, blocked_first):
            first=response(reports=("1001",))
            second=response("UPDATED",reports=("1002",),keys=(KEYS[1],))
            second["supplier_item_ids"]=[ITEM2];second["item_mappings"][0]["supplier_item_id"]=ITEM2
            replies=[first,second] if blocked_first else [second,first]
            values=[candidate(reports=("1001",)),candidate(reports=("1002",))]
            if not blocked_first:values.reverse()
            scope=Scope(replies);scope.agent_store.rows[0]["certifications_jsonb"]=[{"type":"HACCP"}]
            out=service(scope,values).run(CONTEXT)
            self.assertEqual(out["evaluation_item_ids"],[ITEM2])
            for item in out["results"]:
                self.assertIn(item["registration"]["item_mappings"][0]["standardized_item_key"],KEYS)
                if item["registration"]["supplier_item_ids"]==[ITEM1]:
                    self.assertEqual(item["evaluation_item_ids"],[])
                    self.assertIn("TEAM_CERTIFICATION_FORMAT_REQUIRED",item["blockers"])
                else:self.assertEqual(item["evaluation_item_ids"],[ITEM2])
        def test_blocked_then_normal_excludes_only_blocked_evaluation_ids(self):
            self._mixed(True)
        def test_normal_then_blocked_preserves_normal_evaluation_ids(self):
            self._mixed(False)
        def test_no_live_operations_were_attempted(self):
            self.assertEqual(AUDIT,{"network_attempts":0,"credential_file_reads":0})

    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case)
                               for case in (PayloadTests, ResponseTests, PipelineTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("KAN109_V3_OFFLINE_AUDIT=" + json.dumps(AUDIT, sort_keys=True))
    summary = {"pid": os.getpid(), "tests_run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped), "audit": AUDIT}
    print(_RESULT_TAG + json.dumps(summary, sort_keys=True))
    return 0 if result.wasSuccessful() and not any(AUDIT.values()) else 1


class IsolatedKAN109V3Tests(unittest.TestCase):
    def test_import_preserves_environment_path_and_audit_hooks(self):
        with mock.patch.dict(os.environ, {"OPENAI_KAN109_IMPORT_SENTINEL": "SYNTHETIC"}):
            before_environment = dict(os.environ)
            before_path = list(sys.path)
            before_products = {name for name in sys.modules if name.startswith("app.")}
            spec = importlib.util.spec_from_file_location("kan109_import_probe", __file__)
            module = importlib.util.module_from_spec(spec)
            with mock.patch.object(sys, "addaudithook") as audit_hook, mock.patch.object(subprocess, "run") as spawn:
                spec.loader.exec_module(module)
            self.assertTrue(dict(os.environ) == before_environment, "Import changed parent environment")
            self.assertEqual(sys.path, before_path)
            self.assertEqual({name for name in sys.modules if name.startswith("app.")}, before_products)
            audit_hook.assert_not_called()
            spawn.assert_not_called()

    def test_worker_cannot_install_guards_in_importing_process(self):
        before_environment = dict(os.environ)
        before_path = list(sys.path)
        with mock.patch.object(sys, "addaudithook") as audit_hook:
            with self.assertRaisesRegex(RuntimeError, "separate process"):
                _run_worker()
        self.assertTrue(dict(os.environ) == before_environment, "Rejected worker changed parent environment")
        self.assertEqual(sys.path, before_path)
        audit_hook.assert_not_called()

    def test_v3_regressions_run_in_isolated_child(self):
        before_environment = dict(os.environ)
        with mock.patch.object(sys, "addaudithook") as audit_hook:
            result = _run_in_subprocess()
        self.assertTrue(dict(os.environ) == before_environment, "Worker launch changed parent environment")
        audit_hook.assert_not_called()
        self.assertEqual(result.returncode, 0, result.stdout)
        records = [json.loads(line[len(_RESULT_TAG):]) for line in result.stdout.splitlines()
                   if line.startswith(_RESULT_TAG)]
        self.assertEqual(len(records), 1, result.stdout)
        record = records[0]
        self.assertNotEqual(record["pid"], os.getpid())
        self.assertEqual(record["tests_run"], 45)
        self.assertEqual((record["failures"], record["errors"], record["skipped"]), (0, 0, 0))
        self.assertEqual(record["audit"], {"network_attempts": 0, "credential_file_reads": 0})
        print(result.stdout, end="")


if __name__ == "__main__":
    if sys.argv[1:] == [_WORKER_FLAG]:
        raise SystemExit(_run_worker())
    unittest.main()

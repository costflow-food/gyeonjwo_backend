-- Read-only receipt lookup preserves idempotency while the worker/config is unavailable.
-- DB owner review/apply after 004. No tables, business rows or settings changed.
BEGIN;
DO $$ BEGIN
 IF to_regprocedure('public.case_agent_store_health()') IS NULL THEN
  RAISE EXCEPTION 'Apply agent quote store 004 first'; END IF;
END $$;
CREATE FUNCTION public.lookup_work_case_start_request(p_owner uuid,p_case uuid,p_key text,p_hash text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE r internal.api_requests%ROWTYPE;
BEGIN
 PERFORM internal.service_only();
 IF p_owner IS NULL OR p_case IS NULL OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 200
  OR btrim(p_key)<>p_key OR p_key~'[[:cntrl:]]' OR p_hash IS NULL OR p_hash!~'^[0-9a-f]{64}$' THEN
  RAISE EXCEPTION 'invalid request identity' USING ERRCODE='22023'; END IF;
 SELECT * INTO r FROM internal.api_requests WHERE owner_user_id=p_owner AND http_method='POST'
  AND normalized_path='/api/v1/work-cases/'||p_case::text||'/start' AND idempotency_key=p_key;
 IF NOT FOUND THEN RETURN jsonb_build_object('kind','NONE'); END IF;
 IF r.body_sha256<>p_hash THEN RETURN jsonb_build_object('kind','ERROR','code','IDEMPOTENCY_KEY_REUSED'); END IF;
 IF r.status='PROCESSING' THEN RETURN jsonb_build_object('kind','ERROR','code','REQUEST_IN_PROGRESS'); END IF;
 IF r.status<>'COMPLETED' OR r.http_status<>202 OR jsonb_typeof(r.response_jsonb) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'invalid stored acceptance' USING ERRCODE='23514'; END IF;
 RETURN jsonb_build_object('kind','REPLAY','http_status',r.http_status,'response',r.response_jsonb);
END $$;
CREATE FUNCTION public.review_start_health() RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE base jsonb;
BEGIN
 PERFORM internal.service_only();
 base:=public.case_agent_store_health();
 RETURN jsonb_build_object('contract_version',1,'ready',base->>'contract_version'='1' AND base->'ready'='true'::jsonb);
END $$;
REVOKE ALL ON FUNCTION public.lookup_work_case_start_request(uuid,uuid,text,text),public.review_start_health()
 FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.lookup_work_case_start_request(uuid,uuid,text,text),public.review_start_health() TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;

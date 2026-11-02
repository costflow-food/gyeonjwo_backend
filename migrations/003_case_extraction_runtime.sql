-- SQL v3.6 adapter for the existing Python extraction worker. NOT auto-applied.
-- Durable jobs use the existing agent_runs row and private input snapshot.
-- Unknown supplier requests stay in owned normalized mail evidence; never create
-- a supplier or invent a supplier FK just to store an initial request quote.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
SET LOCAL ROLE service_role;
DO $$ BEGIN
 IF public.rfq_schema_health()<>'rfq-reply-facts-v1' THEN
  RAISE EXCEPTION 'Reviewed SQL v3.6 is required before this adapter' USING ERRCODE='55000';END IF;
END $$;
RESET ROLE;

CREATE FUNCTION public.submit_work_case_start(p_owner uuid,p_case uuid,p_revision integer,
 p_key text,p_hash text,p_request_id text) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE claim jsonb; w public.work_cases%ROWTYPE; started jsonb; response jsonb; run_id uuid:=gen_random_uuid();code text;
BEGIN
 PERFORM internal.service_only();
 IF p_owner IS NULL OR p_case IS NULL OR p_revision IS NULL OR p_revision<1 OR p_key IS NULL
  OR length(p_key) NOT BETWEEN 1 AND 200 OR btrim(p_key)<>p_key OR p_key~'[[:cntrl:]]'
  OR p_request_id IS NULL OR p_request_id!~'^[A-Za-z0-9_-]{1,128}$' OR p_hash IS NULL
  OR p_hash<>encode(sha256(convert_to('{"expected_revision":'||p_revision::text||'}','UTF8')),'hex') THEN
  RETURN jsonb_build_object('kind','ERROR','code','ACTION_NOT_ALLOWED');END IF;
 BEGIN
  IF NOT pg_try_advisory_xact_lock(hashtextextended('api:'||p_owner::text||':POST:/api/v1/work-cases/'||p_case::text||'/start:'||p_key,0)) THEN
   RETURN jsonb_build_object('kind','ERROR','code','REQUEST_IN_PROGRESS');END IF;
  claim:=public.begin_api_request(p_owner,'POST','/api/v1/work-cases/'||p_case::text||'/start',p_key,p_hash);
  IF claim->>'action'='REPLAY' THEN RETURN jsonb_build_object('kind','REPLAY','http_status',claim->'http_status','response',claim->'response');
  ELSIF claim->>'action'='REJECT' THEN RETURN jsonb_build_object('kind','ERROR','code',claim->>'code');END IF;
  SELECT * INTO w FROM public.work_cases WHERE owner_user_id=p_owner AND id=p_case FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'RESOURCE_NOT_FOUND' USING ERRCODE='P0002';END IF;
  IF w.input_revision<>p_revision THEN RAISE EXCEPTION 'REVISION_CONFLICT' USING ERRCODE='40001';END IF;
  IF w.status<>'Waiting' OR w.processing_step<>'PENDING_START' OR w.current_batch_id IS NOT NULL THEN
   RAISE EXCEPTION 'WORK_ALREADY_STARTED' USING ERRCODE='55000';END IF;
  IF w.archived_at IS NOT NULL OR w.decision IS NOT NULL THEN RAISE EXCEPTION 'ACTION_NOT_ALLOWED' USING ERRCODE='55000';END IF;
  IF NOT EXISTS(SELECT FROM public.mail_messages WHERE owner_user_id=p_owner AND id=w.initial_mail_id
    AND work_case_id=w.id AND direction='IN' AND mail_classification='BUSINESS' AND link_status='LINKED') THEN
   RAISE EXCEPTION 'ACTION_NOT_ALLOWED' USING ERRCODE='55000';END IF;
  started:=public.start_review(p_owner,p_case,p_revision);
  INSERT INTO public.agent_runs(id,owner_user_id,work_case_id,review_batch_id,source_mail_id,component_type,agent_role,
   input_revision,input_snapshot_jsonb,status,model_id,prompt_version,policy_version)
  VALUES(run_id,p_owner,p_case,(started->>'review_batch_id')::uuid,w.initial_mail_id,'AGENT','INFO_EXTRACTOR',p_revision,
   jsonb_build_object('job_type','case-extraction-v1','job_status','PENDING'),'RUNNING','unassigned','extraction-v1','sql-v3.6');
  UPDATE public.mail_messages SET processing_status='PROCESSING' WHERE owner_user_id=p_owner AND id=w.initial_mail_id;
  response:=jsonb_build_object('data',jsonb_build_object('case_id',p_case,'input_revision',p_revision,
    'batch_id',started->'review_batch_id','progress_url','/api/v1/work-cases/'||p_case::text||'/progress'),
    'meta',jsonb_build_object('request_id',p_request_id));
  PERFORM public.complete_api_request(p_owner,(claim->>'request_id')::uuid,202,response,jsonb_build_object('job_id',run_id));
  RETURN jsonb_build_object('kind','ACCEPTED','http_status',202,'response',response);
 EXCEPTION WHEN SQLSTATE 'P0002' OR SQLSTATE '40001' OR SQLSTATE '55000' OR SQLSTATE 'P0001' OR check_violation THEN
  GET STACKED DIAGNOSTICS code=MESSAGE_TEXT;
  IF code NOT IN ('RESOURCE_NOT_FOUND','REVISION_CONFLICT','WORK_ALREADY_STARTED','ACTION_NOT_ALLOWED') THEN
   code:=CASE SQLSTATE WHEN 'P0002' THEN 'RESOURCE_NOT_FOUND' WHEN '40001' THEN 'REVISION_CONFLICT' ELSE 'ACTION_NOT_ALLOWED' END;
  END IF;
  RETURN jsonb_build_object('kind','ERROR','code',code);
 END;
END $$;

CREATE FUNCTION public.claim_case_extraction() RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE;w public.work_cases%ROWTYPE;token uuid:=gen_random_uuid();
BEGIN
 PERFORM internal.service_only();
 SELECT r0.* INTO r FROM public.agent_runs r0 JOIN public.work_cases w0 ON w0.owner_user_id=r0.owner_user_id AND w0.id=r0.work_case_id
 WHERE r0.agent_role='INFO_EXTRACTOR' AND r0.status='RUNNING'
  AND r0.input_snapshot_jsonb->>'job_type'='case-extraction-v1' AND r0.input_snapshot_jsonb->>'job_status'='PENDING'
 ORDER BY r0.created_at,r0.id LIMIT 1 FOR UPDATE OF w0,r0 SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL;END IF;
 SELECT * INTO STRICT w FROM public.work_cases WHERE owner_user_id=r.owner_user_id AND id=r.work_case_id;
 IF w.status<>'Running' OR w.processing_step<>'EXTRACTING' OR w.archived_at IS NOT NULL
  OR w.input_revision<>r.input_revision OR w.current_batch_id IS DISTINCT FROM r.review_batch_id THEN
  UPDATE public.agent_runs SET status='STALE',finished_at=clock_timestamp(),error_jsonb='{"code":"INPUT_REVISION_CHANGED"}',
   input_snapshot_jsonb=input_snapshot_jsonb||'{"job_status":"STALE"}'::jsonb WHERE id=r.id;
  RETURN NULL;
 END IF;
 UPDATE public.agent_runs SET started_at=clock_timestamp(),input_snapshot_jsonb=input_snapshot_jsonb||
  jsonb_build_object('job_status','PROCESSING','claim_token',token,'lease_expires_at',clock_timestamp()+interval '120 seconds') WHERE id=r.id;
 RETURN jsonb_build_object('job_id',r.id,'claim_token',token,'owner_user_id',r.owner_user_id,'case_id',r.work_case_id,
   'input_revision',r.input_revision,'batch_id',r.review_batch_id,'mail_ids',jsonb_build_array(r.source_mail_id));
END $$;

CREATE FUNCTION public.heartbeat_case_extraction(p_job uuid,p_token uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
 PERFORM internal.service_only();
 SELECT * INTO r FROM public.agent_runs WHERE id=p_job AND agent_role='INFO_EXTRACTOR' AND status='RUNNING'
   AND input_snapshot_jsonb->>'job_type'='case-extraction-v1' FOR UPDATE;
 IF NOT FOUND OR r.input_snapshot_jsonb->>'job_status'<>'PROCESSING'
  OR r.input_snapshot_jsonb->>'claim_token' IS DISTINCT FROM p_token::text
  OR (r.input_snapshot_jsonb->>'lease_expires_at')::timestamptz<=clock_timestamp() THEN RETURN false;END IF;
 IF NOT EXISTS(SELECT FROM public.work_cases w WHERE w.owner_user_id=r.owner_user_id AND w.id=r.work_case_id
  AND w.status='Running' AND w.processing_step='EXTRACTING' AND w.archived_at IS NULL
  AND w.input_revision=r.input_revision AND w.current_batch_id=r.review_batch_id) THEN RETURN false;END IF;
 UPDATE public.agent_runs SET input_snapshot_jsonb=input_snapshot_jsonb||jsonb_build_object('lease_expires_at',clock_timestamp()+interval '120 seconds') WHERE id=r.id;
 RETURN true;
END $$;

CREATE FUNCTION public.complete_case_extraction(p_job uuid,p_token uuid,p_revision integer,p_batch uuid,
 p_output jsonb,p_result jsonb,p_model text,p_prompt_version text) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE;w public.work_cases%ROWTYPE;m public.mail_messages%ROWTYPE;
 entry record; evidence jsonb; evidence_id text; part_ref text; excerpt text;values_ jsonb:='{}';
 supplier_ids uuid[];supplier uuid;qid uuid;prev uuid;version_ integer;quote_ids jsonb:='[]';
 step_ text;status_ text;result_ jsonb;stored jsonb;source text;
BEGIN
 PERFORM internal.service_only();
 SELECT * INTO r FROM public.agent_runs WHERE id=p_job AND agent_role='INFO_EXTRACTOR'
  AND input_snapshot_jsonb->>'job_type'='case-extraction-v1';
 IF NOT FOUND THEN RAISE EXCEPTION 'RESOURCE_NOT_FOUND' USING ERRCODE='P0002';END IF;
 SELECT * INTO STRICT w FROM public.work_cases WHERE owner_user_id=r.owner_user_id AND id=r.work_case_id FOR UPDATE;
 SELECT * INTO STRICT r FROM public.agent_runs WHERE id=p_job FOR UPDATE;
 IF r.input_snapshot_jsonb->>'claim_token' IS DISTINCT FROM p_token::text THEN RAISE EXCEPTION 'lease mismatch' USING ERRCODE='40001';END IF;
 IF r.status='COMPLETED' THEN RETURN jsonb_build_object('kind','STORED','quote_version_ids',coalesce(r.output_jsonb->'quote_version_ids','[]'));END IF;
 IF r.status='STALE' THEN RETURN jsonb_build_object('kind','STALE');END IF;
 IF r.status<>'RUNNING' OR r.input_snapshot_jsonb->>'job_status'<>'PROCESSING'
  OR (r.input_snapshot_jsonb->>'lease_expires_at')::timestamptz<=clock_timestamp() THEN RAISE EXCEPTION 'expired extraction lease' USING ERRCODE='40001';END IF;
 IF p_revision IS DISTINCT FROM r.input_revision OR p_batch IS DISTINCT FROM r.review_batch_id THEN RAISE EXCEPTION 'invalid execution identity' USING ERRCODE='22023';END IF;
 IF w.status<>'Running' OR w.processing_step<>'EXTRACTING' OR w.archived_at IS NOT NULL
  OR w.input_revision<>r.input_revision OR w.current_batch_id IS DISTINCT FROM r.review_batch_id THEN
  UPDATE public.agent_runs SET status='STALE',finished_at=clock_timestamp(),error_jsonb='{"code":"INPUT_REVISION_CHANGED"}',
   input_snapshot_jsonb=input_snapshot_jsonb||'{"job_status":"STALE"}'::jsonb WHERE id=r.id;
  RETURN jsonb_build_object('kind','STALE');END IF;
 SELECT * INTO STRICT m FROM public.mail_messages WHERE owner_user_id=r.owner_user_id AND id=r.source_mail_id AND work_case_id=w.id FOR UPDATE;
 IF p_output->>'status' NOT IN ('EXTRACTION_READY','NEEDS_INFO','ON_HOLD') OR p_output->>'status' IS NULL
  OR p_model IS NULL OR length(p_model) NOT BETWEEN 1 AND 200 OR p_prompt_version IS NULL THEN
  RAISE EXCEPTION 'invalid extraction output' USING ERRCODE='22023';END IF;
 IF p_output->>'status'='ON_HOLD' THEN
  IF p_result IS NOT NULL THEN RAISE EXCEPTION 'hold cannot store staged fields' USING ERRCODE='22023';END IF;
  step_:='ON_HOLD';
  UPDATE public.mail_messages SET processing_status='NEEDS_REVIEW' WHERE id=m.id;
 ELSE
  IF jsonb_typeof(p_result->'fields') IS DISTINCT FROM 'object' OR jsonb_typeof(p_result->'stored_fields') IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'validated fields required' USING ERRCODE='22023';END IF;
  FOR entry IN SELECT key,value FROM jsonb_each(p_result->'fields') LOOP
   IF entry.key NOT IN ('requester_name','item_name','specification','quantity','unit','previous_unit_price','unit_price','effective_from')
    OR jsonb_typeof(entry.value) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'invalid extraction field' USING ERRCODE='22023';END IF;
   IF entry.value->'value' IS NULL OR entry.value->'value'='null'::jsonb THEN values_:=values_||jsonb_build_object(entry.key,NULL);CONTINUE;END IF;
   IF jsonb_typeof(entry.value->'value') IS DISTINCT FROM 'string' OR nullif(btrim(entry.value->>'value'),'') IS NULL
    OR jsonb_typeof(entry.value->'evidence') IS DISTINCT FROM 'array' OR jsonb_array_length(entry.value->'evidence') NOT BETWEEN 1 AND 10 THEN
    RAISE EXCEPTION 'field evidence required' USING ERRCODE='23514';END IF;
   source:='';
   FOR evidence IN SELECT value FROM jsonb_array_elements(entry.value->'evidence') LOOP
    evidence_id:=evidence->>'evidence_id';excerpt:=evidence->>'excerpt';
    IF excerpt IS NULL OR excerpt='' OR length(excerpt)>1500 THEN RAISE EXCEPTION 'invalid evidence excerpt' USING ERRCODE='23514';END IF;
    IF evidence_id=m.id::text||':body' THEN
     IF strpos(m.body_text,excerpt)=0 THEN RAISE EXCEPTION 'body evidence mismatch' USING ERRCODE='23514';END IF;
    ELSIF evidence_id=m.id::text||':subject' THEN
     IF strpos(m.subject,excerpt)=0 THEN RAISE EXCEPTION 'subject evidence mismatch' USING ERRCODE='23514';END IF;
    ELSIF left(evidence_id,length(m.id::text||':part:'))=m.id::text||':part:' THEN
     part_ref:=substr(evidence_id,length(m.id::text||':part:')+1);
     IF NOT EXISTS(SELECT FROM jsonb_array_elements(m.attachments_jsonb) a
       WHERE coalesce(a->>'provider_part_ref',a->>'part_id')=part_ref) THEN RAISE EXCEPTION 'attachment evidence mismatch' USING ERRCODE='23514';END IF;
    ELSE RAISE EXCEPTION 'foreign evidence' USING ERRCODE='23514';END IF;
    source:=source||' '||excerpt;
   END LOOP;
   IF entry.key IN ('quantity','unit_price','previous_unit_price') THEN
    IF entry.value->>'value'!~'^[0-9]+(\.[0-9]{1,6})?$' OR (entry.value->>'value')::numeric>99999999999999.999999
     OR entry.key='quantity' AND (entry.value->>'value')::numeric<=0 THEN RAISE EXCEPTION 'invalid exact number' USING ERRCODE='23514';END IF;
    IF NOT EXISTS(SELECT FROM regexp_matches(replace(source,',',''),'([0-9]+(?:\.[0-9]+)?)','g') x
      WHERE (x[1])::numeric=(entry.value->>'value')::numeric) THEN
     RAISE EXCEPTION 'number evidence mismatch' USING ERRCODE='23514';
    END IF;
   ELSIF entry.key='effective_from' THEN
    IF entry.value->>'value'!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN RAISE EXCEPTION 'invalid date' USING ERRCODE='23514';END IF;
    PERFORM (entry.value->>'value')::date;
   ELSIF strpos(source,entry.value->>'value')=0 THEN RAISE EXCEPTION 'text evidence mismatch' USING ERRCODE='23514';END IF;
   values_:=values_||jsonb_build_object(entry.key,entry.value->>'value');
  END LOOP;
  stored:=p_result->'stored_fields';
  IF (stored-'standardized_item_key') IS DISTINCT FROM values_ THEN RAISE EXCEPTION 'stored fields mismatch' USING ERRCODE='23514';END IF;
  IF values_->>'item_name' IS NOT NULL AND (jsonb_typeof(stored->'standardized_item_key') IS DISTINCT FROM 'string'
    OR nullif(btrim(stored->>'standardized_item_key'),'') IS NULL) THEN RAISE EXCEPTION 'normalized item key required' USING ERRCODE='23514';END IF;
  SELECT array_agg(id ORDER BY id) INTO supplier_ids FROM public.suppliers WHERE owner_user_id=r.owner_user_id
   AND source_type IN ('REGISTERED','VERIFIED_WEB') AND lower(btrim(contact_email))=lower(btrim(m.sender));
  IF cardinality(supplier_ids)=1 THEN supplier:=supplier_ids[1];END IF;
  UPDATE public.mail_messages SET supplier_id=supplier,processing_status='STRUCTURED',
   normalized_jsonb=coalesce(normalized_jsonb,'{}')||jsonb_build_object('extraction_schema','extraction-v1','extraction',p_result,
    'unlinked_request',supplier IS NULL) WHERE id=m.id;
  IF supplier IS NOT NULL THEN
   SELECT id,version_no INTO prev,version_ FROM public.quote_versions WHERE owner_user_id=r.owner_user_id
    AND work_case_id=w.id AND supplier_id=supplier ORDER BY version_no DESC LIMIT 1;
   INSERT INTO public.quote_versions(owner_user_id,work_case_id,supplier_id,source_mail_id,version_no,supersedes_id,
    input_revision,unit_price,unit,effective_from,checks_jsonb,pricing_terms_jsonb,evidence_jsonb,extraction_status)
   VALUES(r.owner_user_id,w.id,supplier,m.id,coalesce(version_,0)+1,prev,r.input_revision,(values_->>'unit_price')::numeric,
    values_->>'unit',(values_->>'effective_from')::date,jsonb_build_object('item_name',values_->'item_name','specification',values_->'specification'),
    '{}',p_result->'fields','NEEDS_REVIEW') RETURNING id INTO qid;
   quote_ids:=jsonb_build_array(qid);
  END IF;
  UPDATE public.work_cases SET requester_name=values_->>'requester_name',item_name=values_->>'item_name',
   standardized_item_key=stored->>'standardized_item_key',specification=values_->>'specification',
   quantity=(values_->>'quantity')::numeric,unit=values_->>'unit',updated_at=clock_timestamp() WHERE id=w.id;
  step_:=CASE WHEN values_->>'item_name' IS NULL THEN 'NEEDS_INFO' ELSE 'CANDIDATES' END;
 END IF;
 PERFORM public.set_review_step(r.owner_user_id,w.id,r.input_revision,r.review_batch_id,step_);
 result_:=p_output||jsonb_build_object('quote_version_ids',quote_ids,'processing_step',step_);
 UPDATE public.agent_runs SET status='COMPLETED',finished_at=clock_timestamp(),model_id=p_model,prompt_version=p_prompt_version,
  output_jsonb=result_,input_snapshot_jsonb=input_snapshot_jsonb||'{"job_status":"DONE"}'::jsonb WHERE id=r.id;
 INSERT INTO public.workflow_logs(owner_user_id,work_case_id,agent_run_id,source_mail_id,event_type,actor_type,actor_id,input_revision,payload_jsonb)
 VALUES(r.owner_user_id,w.id,r.id,m.id,'EXTRACTION_STORED','AGENT','INFO_EXTRACTOR',r.input_revision,
  jsonb_build_object('quote_version_ids',quote_ids,'processing_step',step_,'unlinked_request',supplier IS NULL));
 RETURN jsonb_build_object('kind','STORED','quote_version_ids',quote_ids);
END $$;

CREATE FUNCTION public.fail_case_extraction(p_job uuid,p_token uuid,p_code text) RETURNS boolean
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE;w public.work_cases%ROWTYPE;
BEGIN
 PERFORM internal.service_only();
 IF p_code IS NULL OR p_code NOT IN ('EXTRACTION_FAILED','INPUT_REVISION_CHANGED','WORKER_INTERRUPTED') THEN
  RAISE EXCEPTION 'invalid error code' USING ERRCODE='22023';END IF;
 SELECT * INTO r FROM public.agent_runs WHERE id=p_job AND agent_role='INFO_EXTRACTOR'
   AND input_snapshot_jsonb->>'job_type'='case-extraction-v1';
 IF NOT FOUND THEN RETURN false;END IF;
 SELECT * INTO STRICT w FROM public.work_cases WHERE owner_user_id=r.owner_user_id AND id=r.work_case_id FOR UPDATE;
 SELECT * INTO STRICT r FROM public.agent_runs WHERE id=p_job FOR UPDATE;
 IF r.input_snapshot_jsonb->>'claim_token' IS DISTINCT FROM p_token::text THEN RETURN false;END IF;
 IF r.status IN ('COMPLETED','FAILED','STALE') THEN RETURN true;END IF;
 IF r.input_snapshot_jsonb->>'job_status'<>'PROCESSING' THEN RETURN false;END IF;
 IF p_code='WORKER_INTERRUPTED' AND (r.input_snapshot_jsonb->>'lease_expires_at')::timestamptz>clock_timestamp() THEN RETURN false;END IF;
 IF w.input_revision=r.input_revision AND w.current_batch_id=r.review_batch_id AND w.status='Running'
  AND w.processing_step='EXTRACTING' AND w.archived_at IS NULL THEN
  PERFORM public.set_review_step(r.owner_user_id,w.id,r.input_revision,r.review_batch_id,'FAILED',jsonb_build_object('code',p_code));
  UPDATE public.mail_messages SET processing_status='NEEDS_REVIEW' WHERE owner_user_id=r.owner_user_id AND id=r.source_mail_id;
  UPDATE public.agent_runs SET status='FAILED',finished_at=clock_timestamp(),error_jsonb=jsonb_build_object('code',p_code),
   input_snapshot_jsonb=input_snapshot_jsonb||'{"job_status":"FAILED"}'::jsonb WHERE id=r.id;
 ELSE
  UPDATE public.agent_runs SET status='STALE',finished_at=clock_timestamp(),error_jsonb='{"code":"INPUT_REVISION_CHANGED"}',
   input_snapshot_jsonb=input_snapshot_jsonb||'{"job_status":"STALE"}'::jsonb WHERE id=r.id;
 END IF;
 RETURN true;
END $$;

CREATE FUNCTION public.recover_case_extractions() RETURNS integer
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE row_ record; n integer:=0;
BEGIN
 PERFORM internal.service_only();
 FOR row_ IN SELECT id,(input_snapshot_jsonb->>'claim_token')::uuid token FROM public.agent_runs
  WHERE agent_role='INFO_EXTRACTOR' AND status='RUNNING' AND input_snapshot_jsonb->>'job_type'='case-extraction-v1'
  AND input_snapshot_jsonb->>'job_status'='PROCESSING'
  AND (input_snapshot_jsonb->>'lease_expires_at')::timestamptz<=clock_timestamp() ORDER BY started_at,id LIMIT 100 LOOP
  IF public.fail_case_extraction(row_.id,row_.token,'WORKER_INTERRUPTED') THEN n:=n+1;END IF;
 END LOOP;
 RETURN n;
END $$;

CREATE FUNCTION public.case_extraction_health() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE signature text;
BEGIN
 PERFORM internal.service_only();
 FOREACH signature IN ARRAY ARRAY['public.submit_work_case_start(uuid,uuid,integer,text,text,text)',
  'public.claim_case_extraction()','public.heartbeat_case_extraction(uuid,uuid)',
  'public.complete_case_extraction(uuid,uuid,integer,uuid,jsonb,jsonb,text,text)',
  'public.fail_case_extraction(uuid,uuid,text)','public.recover_case_extractions()'] LOOP
  IF to_regprocedure(signature) IS NULL OR NOT has_function_privilege('service_role',signature,'EXECUTE')
   OR has_function_privilege('anon',signature,'EXECUTE') OR has_function_privilege('authenticated',signature,'EXECUTE') THEN
   RETURN jsonb_build_object('contract_version',1,'ready',false);END IF;
 END LOOP;
 RETURN jsonb_build_object('contract_version',1,'ready',true);
END $$;

DO $$ DECLARE signature text;
BEGIN
 FOREACH signature IN ARRAY ARRAY['public.submit_work_case_start(uuid,uuid,integer,text,text,text)',
  'public.claim_case_extraction()','public.heartbeat_case_extraction(uuid,uuid)',
  'public.complete_case_extraction(uuid,uuid,integer,uuid,jsonb,jsonb,text,text)',
  'public.fail_case_extraction(uuid,uuid,text)','public.recover_case_extractions()','public.case_extraction_health()'] LOOP
  EXECUTE 'REVOKE ALL ON FUNCTION '||signature||' FROM PUBLIC,anon,authenticated';
  EXECUTE 'GRANT EXECUTE ON FUNCTION '||signature||' TO service_role';
 END LOOP;
END $$;
COMMIT;

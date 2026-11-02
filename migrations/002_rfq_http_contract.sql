-- Additive server HTTP/worker contract on SQL v3.6. DB-owner review required.
-- Never run the full setup on an existing database. This file is NOT auto-applied.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
SET LOCAL ROLE service_role;
DO $$ BEGIN
 IF public.rfq_schema_health()<>'rfq-reply-facts-v1' THEN
  RAISE EXCEPTION 'Reviewed SQL v3.6 is required before this adapter' USING ERRCODE='55000';END IF;
END $$;
RESET ROLE;

CREATE FUNCTION internal.rfq_draft_snapshot_hash(d public.mail_drafts) RETURNS text
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT encode(sha256(convert_to(string_agg(octet_length(v)::text||':'||v,'' ORDER BY n),'UTF8')),'hex')
 FROM unnest(ARRAY[d.id::text,d.work_case_id::text,d.supplier_id::text,d.intent_id::text,d.purpose,
                    d.recipient,d.subject,d.body_text,d.input_revision::text]) WITH ORDINALITY a(v,n)
$$;

CREATE FUNCTION public.submit_rfq_draft_action(
 p_owner uuid,p_draft uuid,p_action text,p_revision integer,p_snapshot text,p_settings_version text,
 p_key text,p_hash text,p_request_id text,p_accepting boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE claim jsonb; d public.mail_drafts%ROWTYPE; w public.work_cases%ROWTYPE;
 base public.mail_send_settings%ROWTYPE; ov public.mail_send_settings%ROWTYPE; chosen public.mail_send_settings%ROWTYPE;
 response jsonb; suffix text; canonical text; err text; code text;
BEGIN
 PERFORM internal.service_only();
 IF p_owner IS NULL OR p_draft IS NULL OR p_action IS NULL OR p_action NOT IN ('APPROVE_SEND','RECONCILE')
 OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 200 OR btrim(p_key)<>p_key OR p_key~'[[:cntrl:]]'
 OR p_hash IS NULL OR p_hash!~'^[0-9a-f]{64}$' OR p_request_id IS NULL OR p_request_id!~'^[A-Za-z0-9_-]{1,128}$'
 OR p_accepting IS NULL THEN RETURN jsonb_build_object('kind','ERROR','code','ACTION_NOT_ALLOWED'); END IF;
 IF p_action='APPROVE_SEND' THEN
  IF p_revision IS NULL OR p_revision<1 OR p_snapshot IS NULL OR p_snapshot!~'^[0-9a-f]{64}$'
   OR p_settings_version IS NULL OR p_settings_version!~'^(none|[0-9a-f-]{36}):(none|[0-9a-f-]{36})$'
  THEN RETURN jsonb_build_object('kind','ERROR','code','ACTION_NOT_ALLOWED'); END IF;
  suffix:='approve-and-send';
  canonical:='{"expected_revision":'||p_revision::text||',"expected_settings_version":'||to_json(p_settings_version)::text||',"snapshot_hash":'||to_json(p_snapshot)::text||'}';
 ELSE
  suffix:='reconcile';canonical:='{}';
 END IF;
 IF encode(sha256(convert_to(canonical,'UTF8')),'hex')<>p_hash THEN
  RETURN jsonb_build_object('kind','ERROR','code','ACTION_NOT_ALLOWED'); END IF;
 BEGIN
  IF NOT pg_try_advisory_xact_lock(hashtextextended('api:'||p_owner::text||':POST:/api/v1/mail-drafts/'||p_draft::text||'/'||suffix||':'||p_key,0)) THEN
   RETURN jsonb_build_object('kind','ERROR','code','REQUEST_IN_PROGRESS');END IF;
  claim:=public.begin_api_request(p_owner,'POST','/api/v1/mail-drafts/'||p_draft::text||'/'||suffix,p_key,p_hash);
  IF claim->>'action'='REPLAY' THEN
   RETURN jsonb_build_object('kind','REPLAY','http_status',claim->'http_status','response',claim->'response');
  ELSIF claim->>'action'='REJECT' THEN
   RETURN jsonb_build_object('kind','ERROR','code',claim->>'code');
  END IF;
  SELECT * INTO d FROM public.mail_drafts WHERE owner_user_id=p_owner AND id=p_draft;
  IF NOT FOUND THEN RAISE EXCEPTION 'RESOURCE_NOT_FOUND' USING ERRCODE='P0002'; END IF;
  -- Replays remain available after a worker is disabled. Foreign and missing
  -- drafts still have the same 404, independent of worker configuration.
  IF NOT p_accepting THEN RAISE EXCEPTION 'PROCESSING_UNAVAILABLE' USING ERRCODE='55000'; END IF;
  SELECT * INTO STRICT w FROM public.work_cases WHERE owner_user_id=p_owner AND id=d.work_case_id FOR UPDATE;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_owner::text||':'||w.mailbox_ref,0));
  SELECT * INTO STRICT d FROM public.mail_drafts WHERE owner_user_id=p_owner AND id=p_draft FOR UPDATE;
  IF d.purpose<>'RFQ' OR d.style_snapshot_jsonb->>'producer' IS DISTINCT FROM 'rfq-v1' THEN
   RAISE EXCEPTION 'ACTION_NOT_ALLOWED' USING ERRCODE='55000'; END IF;
  IF p_action='APPROVE_SEND' THEN
   IF w.input_revision<>p_revision OR d.input_revision<>p_revision THEN
    RAISE EXCEPTION 'REVISION_CONFLICT' USING ERRCODE='40001'; END IF;
   IF internal.rfq_draft_snapshot_hash(d)<>p_snapshot THEN RAISE EXCEPTION 'DRAFT_CHANGED' USING ERRCODE='40001'; END IF;
   IF internal.policy_version(p_owner,w.mailbox_ref,w.id)<>p_settings_version THEN
    RAISE EXCEPTION 'SETTINGS_CHANGED' USING ERRCODE='40001'; END IF;
   IF d.status='UNKNOWN' THEN RAISE EXCEPTION 'SEND_RESULT_UNKNOWN' USING ERRCODE='55000'; END IF;
   IF d.status<>'DRAFT' OR w.archived_at IS NOT NULL OR internal.work_case_is_accepted(w)
    OR coalesce(w.decision='HOLD',false) OR NOT (w.status='Pending' AND w.processing_step='WAITING_APPROVAL'
      OR w.status='Running' AND w.processing_step IN ('CANDIDATES','WAITING_REPLY')) THEN
    RAISE EXCEPTION 'ACTION_NOT_ALLOWED' USING ERRCODE='55000'; END IF;
   IF EXISTS(SELECT FROM public.mail_drafts WHERE owner_user_id=p_owner AND replaces_id=d.id) THEN
    RAISE EXCEPTION 'DRAFT_CHANGED' USING ERRCODE='40001'; END IF;
   IF NOT EXISTS(SELECT FROM public.agent_runs r WHERE r.owner_user_id=p_owner AND r.work_case_id=w.id
      AND r.id=(d.style_snapshot_jsonb->>'producer_run_id')::uuid AND r.agent_role='CANDIDATE'
      AND r.input_revision=p_revision AND r.status='COMPLETED') THEN
    RAISE EXCEPTION 'ACTION_NOT_ALLOWED' USING ERRCODE='55000'; END IF;
   PERFORM internal.rfq_check_send(w,d);
   SELECT * INTO base FROM public.mail_send_settings WHERE owner_user_id=p_owner AND mailbox_ref=w.mailbox_ref
    AND work_case_id IS NULL AND effective_at<=clock_timestamp() ORDER BY version_no DESC LIMIT 1;
   SELECT * INTO ov FROM public.mail_send_settings WHERE owner_user_id=p_owner AND mailbox_ref=w.mailbox_ref
    AND work_case_id=w.id AND effective_at<=clock_timestamp() ORDER BY version_no DESC LIMIT 1;
   IF ov.id IS NOT NULL AND ov.approval_required_by_purpose ? 'RFQ' THEN chosen:=ov;ELSE chosen:=base;END IF;
   IF chosen.id IS NULL OR NOT chosen.configured OR jsonb_typeof(chosen.approval_required_by_purpose->'RFQ') IS DISTINCT FROM 'boolean'
    OR NOT chosen.allowed_recipients_jsonb @> jsonb_build_array(d.recipient)
    OR NOT coalesce(chosen.allowed_scope_jsonb->'purposes' @> '["RFQ"]'::jsonb,false)
    OR NOT coalesce(chosen.allowed_scope_jsonb->'work_case_ids' @> jsonb_build_array(w.id::text),false)
   THEN RAISE EXCEPTION 'SEND_SCOPE_REQUIRED' USING ERRCODE='55000'; END IF;
   IF (chosen.approval_required_by_purpose->>'RFQ')::boolean IS NOT TRUE THEN
    RAISE EXCEPTION 'SETTINGS_CHANGED' USING ERRCODE='40001'; END IF;
   IF w.status='Running' THEN
    PERFORM public.set_review_step(p_owner,w.id,p_revision,w.current_batch_id,'WAITING_APPROVAL');
   END IF;
   PERFORM public.approve_mail_draft(p_owner,w.id,p_revision,d.id);
   -- The approved DRAFT itself is durable work scanned by the sender. No Gmail
   -- operation occurs in this transaction or in the HTTP request handler.
   INSERT INTO public.workflow_logs(owner_user_id,work_case_id,event_type,actor_type,actor_id,input_revision,payload_jsonb)
    VALUES(p_owner,w.id,'RFQ_DISPATCH_REQUESTED','USER',p_owner::text,p_revision,jsonb_build_object('draft_id',d.id));
  ELSE
   IF d.status<>'UNKNOWN' THEN RAISE EXCEPTION 'ACTION_NOT_ALLOWED' USING ERRCODE='55000'; END IF;
   INSERT INTO public.workflow_logs(id,owner_user_id,work_case_id,event_type,actor_type,actor_id,input_revision,payload_jsonb)
    VALUES((claim->>'request_id')::uuid,p_owner,w.id,'RFQ_RECONCILE_REQUESTED','USER',p_owner::text,w.input_revision,
     jsonb_build_object('draft_id',d.id));
  END IF;
  response:=jsonb_build_object('data',jsonb_build_object('draft_id',d.id,'accepted',true,
     'draft_url','/api/v1/mail-drafts/'||d.id::text),'meta',jsonb_build_object('request_id',p_request_id));
  PERFORM public.complete_api_request(p_owner,(claim->>'request_id')::uuid,202,response,jsonb_build_object('draft_id',d.id));
  RETURN jsonb_build_object('kind','ACCEPTED','http_status',202,'response',response);
 EXCEPTION WHEN SQLSTATE 'P0002' OR SQLSTATE '40001' OR SQLSTATE '55000' OR SQLSTATE 'P0001'
   OR foreign_key_violation OR check_violation OR invalid_parameter_value THEN
  GET STACKED DIAGNOSTICS err=MESSAGE_TEXT;
  IF err IN ('RESOURCE_NOT_FOUND','REVISION_CONFLICT','DRAFT_CHANGED','SETTINGS_CHANGED','SEND_RESULT_UNKNOWN',
             'ACTION_NOT_ALLOWED','SEND_SCOPE_REQUIRED','PROCESSING_UNAVAILABLE') THEN code:=err;
  ELSIF SQLSTATE='P0002' THEN code:='RESOURCE_NOT_FOUND';
  ELSIF SQLSTATE='40001' THEN code:='REVISION_CONFLICT';
  ELSE code:='ACTION_NOT_ALLOWED';END IF;
  RETURN jsonb_build_object('kind','ERROR','code',code);
 END;
END $$;

CREATE FUNCTION public.list_rfq_reconcile_requests(p_owner uuid,p_limit integer DEFAULT 50) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
BEGIN
 PERFORM internal.service_only();
 IF p_owner IS NULL OR p_limit NOT BETWEEN 1 AND 100 OR p_limit IS NULL THEN
  RAISE EXCEPTION 'invalid reconcile read limit' USING ERRCODE='22023';END IF;
 RETURN (SELECT coalesce(jsonb_agg(jsonb_build_object('event_id',id,'draft_id',draft_id) ORDER BY created_at,id),'[]'::jsonb)
 FROM (SELECT l.id,l.created_at,l.payload_jsonb->>'draft_id' draft_id FROM public.workflow_logs l
  JOIN public.mail_drafts d ON d.id=(l.payload_jsonb->>'draft_id')::uuid AND d.owner_user_id=l.owner_user_id
  WHERE l.owner_user_id=p_owner AND l.event_type='RFQ_RECONCILE_REQUESTED'
   AND NOT EXISTS(SELECT FROM public.workflow_logs done WHERE done.owner_user_id=p_owner
      AND done.event_type='RFQ_RECONCILE_COMPLETED' AND done.payload_jsonb->>'request_event_id'=l.id::text)
  ORDER BY l.created_at,l.id LIMIT p_limit) jobs);
END $$;

CREATE FUNCTION public.finish_rfq_reconcile_request(p_owner uuid,p_event uuid,p_code text DEFAULT NULL) RETURNS boolean
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE e public.workflow_logs%ROWTYPE; d public.mail_drafts%ROWTYPE;
BEGIN
 PERFORM internal.service_only();
 SELECT * INTO e FROM public.workflow_logs WHERE owner_user_id=p_owner AND id=p_event
  AND event_type='RFQ_RECONCILE_REQUESTED' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'RESOURCE_NOT_FOUND' USING ERRCODE='P0002';END IF;
 IF p_code IS NOT NULL AND p_code<>'RECONCILE_UNAVAILABLE' THEN RAISE EXCEPTION 'invalid result code' USING ERRCODE='22023';END IF;
 IF EXISTS(SELECT FROM public.workflow_logs WHERE owner_user_id=p_owner AND event_type='RFQ_RECONCILE_COMPLETED'
   AND payload_jsonb->>'request_event_id'=p_event::text) THEN RETURN true;END IF;
 SELECT * INTO STRICT d FROM public.mail_drafts WHERE owner_user_id=p_owner AND id=(e.payload_jsonb->>'draft_id')::uuid;
 INSERT INTO public.workflow_logs(owner_user_id,work_case_id,event_type,actor_type,actor_id,input_revision,payload_jsonb)
 VALUES(p_owner,d.work_case_id,'RFQ_RECONCILE_COMPLETED','NODE','rfq_sender',d.input_revision,
  jsonb_build_object('request_event_id',p_event,'draft_id',d.id,'status',d.status,'code',p_code));
 RETURN true;
END $$;

CREATE FUNCTION public.recover_rfq_server(p_owner uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE row_ record; r public.agent_runs%ROWTYPE; d public.mail_drafts%ROWTYPE; w public.work_cases%ROWTYPE;
 cutoff timestamptz:=clock_timestamp()-interval '5 minutes'; runs_count integer:=0;sends_count integer:=0;
BEGIN
 PERFORM internal.service_only();
 IF p_owner IS NULL THEN RAISE EXCEPTION 'owner required' USING ERRCODE='22023';END IF;
 FOR row_ IN SELECT id,work_case_id FROM public.agent_runs WHERE owner_user_id=p_owner AND agent_role='CANDIDATE'
  AND prompt_version='candidate-rfq-v1' AND status='RUNNING' AND started_at<cutoff ORDER BY started_at,id LIMIT 50 LOOP
  SELECT * INTO w FROM public.work_cases WHERE owner_user_id=p_owner AND id=row_.work_case_id FOR UPDATE;
  SELECT * INTO r FROM public.agent_runs WHERE owner_user_id=p_owner AND id=row_.id FOR UPDATE;
  IF r.status<>'RUNNING' OR r.started_at>=cutoff THEN CONTINUE;END IF;
  IF w.input_revision=r.input_revision AND w.current_batch_id=r.review_batch_id THEN
   IF w.status='Running' AND w.archived_at IS NULL THEN
    PERFORM public.set_review_step(p_owner,w.id,w.input_revision,w.current_batch_id,'FAILED',jsonb_build_object('code','WORKER_INTERRUPTED'));
   END IF;
   UPDATE public.agent_runs SET status='FAILED',error_jsonb=jsonb_build_object('code','WORKER_INTERRUPTED'),finished_at=clock_timestamp() WHERE id=r.id;
  ELSE
   UPDATE public.agent_runs SET status='STALE',error_jsonb=jsonb_build_object('code','INPUT_REVISION_CHANGED'),finished_at=clock_timestamp() WHERE id=r.id;
  END IF;
  runs_count:=runs_count+1;
 END LOOP;
 FOR row_ IN SELECT id,work_case_id FROM public.mail_drafts draft WHERE owner_user_id=p_owner AND purpose='RFQ'
  AND status='SENDING' AND style_snapshot_jsonb->>'producer'='rfq-v1'
  AND (SELECT max(l.created_at) FROM public.workflow_logs l WHERE l.owner_user_id=p_owner
   AND l.work_case_id=draft.work_case_id AND l.event_type IN ('APPROVED','AUTO_SEND_AUTHORIZED')
   AND l.payload_jsonb->>'draft_id'=draft.id::text)<cutoff ORDER BY id LIMIT 50 LOOP
  SELECT * INTO w FROM public.work_cases WHERE owner_user_id=p_owner AND id=row_.work_case_id FOR UPDATE;
  SELECT * INTO d FROM public.mail_drafts WHERE owner_user_id=p_owner AND id=row_.id FOR UPDATE;
  IF d.status<>'SENDING' THEN CONTINUE;END IF;
  PERFORM public.finish_mail_send(p_owner,d.id,'UNKNOWN',NULL,jsonb_build_object('code','SEND_RESULT_UNKNOWN'));
  sends_count:=sends_count+1;
 END LOOP;
 RETURN jsonb_build_object('interrupted_runs',runs_count,'uncertain_sends',sends_count);
END $$;

CREATE FUNCTION public.rfq_http_health() RETURNS text LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE signature text;
BEGIN
 PERFORM internal.service_only();
 FOREACH signature IN ARRAY ARRAY['public.submit_rfq_draft_action(uuid,uuid,text,integer,text,text,text,text,text,boolean)',
  'public.list_rfq_reconcile_requests(uuid,integer)','public.finish_rfq_reconcile_request(uuid,uuid,text)',
  'public.recover_rfq_server(uuid)','internal.rfq_draft_snapshot_hash(public.mail_drafts)'] LOOP
  IF to_regprocedure(signature) IS NULL OR NOT has_function_privilege('service_role',signature,'EXECUTE')
   OR has_function_privilege('anon',signature,'EXECUTE') OR has_function_privilege('authenticated',signature,'EXECUTE') THEN
   RAISE EXCEPTION 'RFQ HTTP contract unavailable' USING ERRCODE='55000';END IF;
 END LOOP;
 RETURN 'rfq-http-v1';
END $$;

REVOKE ALL ON FUNCTION internal.rfq_draft_snapshot_hash(public.mail_drafts) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION internal.rfq_draft_snapshot_hash(public.mail_drafts) TO service_role;
REVOKE ALL ON FUNCTION public.submit_rfq_draft_action(uuid,uuid,text,integer,text,text,text,text,text,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.submit_rfq_draft_action(uuid,uuid,text,integer,text,text,text,text,text,boolean) TO service_role;
REVOKE ALL ON FUNCTION public.list_rfq_reconcile_requests(uuid,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.list_rfq_reconcile_requests(uuid,integer) TO service_role;
REVOKE ALL ON FUNCTION public.finish_rfq_reconcile_request(uuid,uuid,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.finish_rfq_reconcile_request(uuid,uuid,text) TO service_role;
REVOKE ALL ON FUNCTION public.recover_rfq_server(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.recover_rfq_server(uuid) TO service_role;
REVOKE ALL ON FUNCTION public.rfq_http_health() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.rfq_http_health() TO service_role;
COMMIT;

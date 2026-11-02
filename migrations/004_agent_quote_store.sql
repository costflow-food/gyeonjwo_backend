-- Backend implementation of the agent team's QuoteStore. DB owner review/apply only.
-- Requires base v3.6 and local additive migrations 002/003. No agent sources changed.
BEGIN;
DO $$ BEGIN
 IF to_regprocedure('public.complete_case_extraction(uuid,uuid,integer,uuid,jsonb,jsonb,text,text)') IS NULL THEN
  RAISE EXCEPTION 'Apply extraction runtime 003 first'; END IF;
END $$;

CREATE FUNCTION internal.lock_case_agent_job(p_job uuid,p_token uuid,p_revision integer)
RETURNS public.agent_runs LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE; w public.work_cases%ROWTYPE;
BEGIN
 PERFORM internal.service_only();
 SELECT * INTO r FROM public.agent_runs WHERE id=p_job AND agent_role='INFO_EXTRACTOR'
  AND input_snapshot_jsonb->>'job_type'='case-extraction-v1';
 IF NOT FOUND THEN RAISE EXCEPTION 'RESOURCE_NOT_FOUND' USING ERRCODE='P0002'; END IF;
 SELECT * INTO STRICT w FROM public.work_cases WHERE owner_user_id=r.owner_user_id AND id=r.work_case_id FOR UPDATE;
 SELECT * INTO STRICT r FROM public.agent_runs WHERE id=p_job FOR UPDATE;
 IF p_token IS NULL OR r.input_snapshot_jsonb->>'claim_token' IS DISTINCT FROM p_token::text
  OR r.status IS DISTINCT FROM 'RUNNING' OR r.input_snapshot_jsonb->>'job_status' IS DISTINCT FROM 'PROCESSING'
  OR r.input_snapshot_jsonb->>'lease_expires_at' IS NULL
  OR (r.input_snapshot_jsonb->>'lease_expires_at')::timestamptz<=clock_timestamp() THEN
  RAISE EXCEPTION 'execution lease lost' USING ERRCODE='40001'; END IF;
 IF p_revision IS DISTINCT FROM r.input_revision OR w.input_revision<>r.input_revision
  OR w.current_batch_id IS DISTINCT FROM r.review_batch_id OR w.status<>'Running'
  OR w.processing_step<>'EXTRACTING' OR w.archived_at IS NOT NULL THEN
  RAISE EXCEPTION 'input revision changed' USING ERRCODE='40001'; END IF;
 RETURN r;
END $$;

CREATE FUNCTION internal.agent_exact_number(p_value jsonb) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE v text;
BEGIN
 IF p_value IS NULL OR p_value='null'::jsonb THEN RETURN NULL; END IF;
 v:=p_value#>>'{}';
 IF jsonb_typeof(p_value)<>'string' OR v!~'^[0-9]+(\.[0-9]{1,6})?$'
  OR v::numeric>99999999999999.999999 THEN
  RAISE EXCEPTION 'invalid exact number' USING ERRCODE='22023'; END IF;
 RETURN v::numeric;
END $$;

CREATE FUNCTION public.begin_case_agent_run(p_job uuid,p_token uuid,p_revision integer,p_model text,p_prompt_version text,p_input jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
 r:=internal.lock_case_agent_job(p_job,p_token,p_revision);
 IF nullif(p_model,'') IS NULL OR length(p_model)>200 OR nullif(p_prompt_version,'') IS NULL
  OR p_input->>'case_id' IS DISTINCT FROM r.work_case_id::text
  OR (p_input->>'input_revision')::integer IS DISTINCT FROM r.input_revision
  OR p_input->'mail_ids' IS DISTINCT FROM jsonb_build_array(r.source_mail_id::text) THEN
  RAISE EXCEPTION 'invalid agent run metadata' USING ERRCODE='22023'; END IF;
 UPDATE public.agent_runs SET model_id=p_model,prompt_version=p_prompt_version,
  input_snapshot_jsonb=input_snapshot_jsonb||jsonb_build_object('agent_input',p_input) WHERE id=r.id;
 RETURN true;
END $$;

CREATE FUNCTION public.store_case_agent_quote(p_job uuid,p_token uuid,p_revision integer,p_record jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE; w public.work_cases%ROWTYPE; m public.mail_messages%ROWTYPE;
 s public.suppliers%ROWTYPE; old public.quote_versions%ROWTYPE; q public.quote_versions%ROWTYPE;
 e record; n record; note jsonb; previous jsonb; normalized jsonb; item text; spec text; iid uuid;ids uuid[];
BEGIN
 r:=internal.lock_case_agent_job(p_job,p_token,p_revision);
 SELECT * INTO STRICT w FROM public.work_cases WHERE id=r.work_case_id AND owner_user_id=r.owner_user_id;
 IF jsonb_typeof(p_record) IS DISTINCT FROM 'object' OR p_record->>'work_case_id' IS DISTINCT FROM w.id::text
  OR p_record->>'source_mail_id' IS DISTINCT FROM r.source_mail_id::text
  OR p_record->>'currency' IS DISTINCT FROM 'KRW'
  OR p_record->>'extraction_status' NOT IN ('NEEDS_REVIEW','CONFIRMED','UNSUPPORTED') THEN
  RAISE EXCEPTION 'unsupported quote identity or currency' USING ERRCODE='22023'; END IF;
 SELECT * INTO STRICT m FROM public.mail_messages WHERE owner_user_id=r.owner_user_id
  AND id=r.source_mail_id AND work_case_id=w.id FOR UPDATE;
 SELECT * INTO s FROM public.suppliers WHERE owner_user_id=r.owner_user_id
  AND id=(p_record->>'supplier_id')::uuid AND source_type IN ('REGISTERED','VERIFIED_WEB');
 IF NOT FOUND OR (m.supplier_id IS NOT NULL AND m.supplier_id<>s.id) THEN
  RAISE EXCEPTION 'supplier scope mismatch' USING ERRCODE='23514'; END IF;
 IF m.supplier_id IS NULL AND lower(btrim(s.contact_email)) IS DISTINCT FROM lower(btrim(m.sender))
  AND p_record#>>ARRAY['mail_normalized',m.id::text,'fields','supplier_name','value'] IS DISTINCT FROM s.name THEN
  RAISE EXCEPTION 'supplier evidence required' USING ERRCODE='23514'; END IF;
 IF jsonb_typeof(p_record->'evidence_jsonb') IS DISTINCT FROM 'object'
  OR jsonb_typeof(p_record->'checks_jsonb') IS DISTINCT FROM 'object'
  OR jsonb_typeof(p_record->'pricing_terms_jsonb') IS DISTINCT FROM 'object'
  OR jsonb_typeof(p_record->'mail_normalized') IS DISTINCT FROM 'object'
  OR jsonb_typeof(p_record->'cost_terms_jsonb') IS DISTINCT FROM 'array'
  OR jsonb_typeof(p_record->'item_notes_jsonb') IS DISTINCT FROM 'array' THEN
  RAISE EXCEPTION 'invalid full quote shape' USING ERRCODE='22023'; END IF;
 -- Every evidence mail must belong to the same owner/case. Carried evidence
 -- remains allowed, but never evidence from another user's message.
 FOR e IN SELECT value FROM jsonb_each(p_record->'evidence_jsonb') LOOP
  IF NOT EXISTS(SELECT FROM public.mail_messages WHERE id=(e.value->>'mail_id')::uuid
    AND owner_user_id=r.owner_user_id AND work_case_id=w.id) THEN
   RAISE EXCEPTION 'foreign quote evidence' USING ERRCODE='23514'; END IF;
 END LOOP;
 FOR note IN SELECT value FROM jsonb_array_elements((p_record->'cost_terms_jsonb')||(p_record->'item_notes_jsonb')) LOOP
  IF NOT EXISTS(SELECT FROM public.mail_messages WHERE id=(note->>'mail_id')::uuid
    AND owner_user_id=r.owner_user_id AND work_case_id=w.id) THEN
   RAISE EXCEPTION 'foreign quote evidence' USING ERRCODE='23514'; END IF;
 END LOOP;
 SELECT * INTO old FROM public.quote_versions WHERE owner_user_id=r.owner_user_id
  AND work_case_id=w.id AND supplier_id=s.id ORDER BY version_no DESC LIMIT 1;
 IF old.id IS NOT NULL AND old.source_mail_id=m.id
  AND (old.pricing_terms_jsonb->'agent_record')-ARRAY['version_no','supersedes_id','case_conditions','evidence_jsonb','mail_normalized']
   IS NOT DISTINCT FROM p_record-ARRAY['version_no','supersedes_id','case_conditions','evidence_jsonb','mail_normalized'] THEN
  RETURN jsonb_build_object('kind','STORED','quote_id',old.id,'record',old.pricing_terms_jsonb->'agent_record'); END IF;
 IF (p_record->>'version_no')::integer IS DISTINCT FROM coalesce(old.version_no,0)+1
  OR (p_record->>'supersedes_id')::uuid IS DISTINCT FROM old.id THEN
  RAISE EXCEPTION 'quote version changed' USING ERRCODE='40001'; END IF;
 item:=coalesce(nullif(w.item_name,''),p_record#>>'{case_conditions,item_name}');
 spec:=coalesce(w.specification,p_record#>>'{case_conditions,specification}');
 SELECT array_agg(id) INTO ids FROM public.supplier_items WHERE owner_user_id=r.owner_user_id
  AND supplier_id=s.id AND item_name=item AND specification IS NOT DISTINCT FROM spec;
 IF cardinality(ids)=1 THEN iid:=ids[1]; END IF;
 INSERT INTO public.quote_versions(owner_user_id,work_case_id,supplier_id,source_mail_id,supplier_item_id,
  version_no,supersedes_id,input_revision,unit_price,unit,shipping_cost,available_quantity,effective_from,valid_until,
  checks_jsonb,pricing_terms_jsonb,cost_terms_jsonb,item_notes_jsonb,evidence_jsonb,extraction_status,currency,
  moq_quantity,moq_unit,order_increment_quantity,order_increment_unit)
 VALUES(r.owner_user_id,w.id,s.id,m.id,iid,(p_record->>'version_no')::integer,old.id,r.input_revision,
  internal.agent_exact_number(p_record->'unit_price'),p_record->>'unit',internal.agent_exact_number(p_record->'shipping_cost'),
  internal.agent_exact_number(p_record->'available_quantity'),(p_record->>'effective_from')::date,(p_record->>'valid_until')::date,
  p_record->'checks_jsonb',(p_record->'pricing_terms_jsonb')||jsonb_build_object('agent_record',p_record),
  jsonb_build_object('items',p_record->'cost_terms_jsonb'),jsonb_build_object('items',p_record->'item_notes_jsonb'),
  p_record->'evidence_jsonb',p_record->>'extraction_status','KRW',
  internal.agent_exact_number(p_record#>'{pricing_terms_jsonb,moq}'),p_record#>>'{pricing_terms_jsonb,moq_unit}',
  internal.agent_exact_number(p_record#>'{pricing_terms_jsonb,pack_multiple}'),p_record#>>'{pricing_terms_jsonb,pack_multiple_unit}')
 RETURNING * INTO q;
 FOR n IN SELECT key,value FROM jsonb_each(p_record->'mail_normalized') LOOP
  SELECT normalized_jsonb INTO previous FROM public.mail_messages
   WHERE id=n.key::uuid AND owner_user_id=r.owner_user_id AND work_case_id=w.id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'foreign normalized mail' USING ERRCODE='23514'; END IF;
  normalized:=coalesce(previous,'{}')||n.value||jsonb_build_object(
   'fields',coalesce(previous->'fields','{}')||coalesce(n.value->'fields','{}'),
   'item_notes',coalesce(previous->'item_notes','[]'));
  FOR note IN SELECT value FROM jsonb_array_elements(coalesce(n.value->'item_notes','[]')) LOOP
   IF NOT normalized->'item_notes' @> jsonb_build_array(note) THEN
    normalized:=jsonb_set(normalized,'{item_notes}',(normalized->'item_notes')||jsonb_build_array(note)); END IF;
  END LOOP;
  UPDATE public.mail_messages SET normalized_jsonb=normalized,processing_status='STRUCTURED',supplier_id=s.id
   WHERE id=n.key::uuid AND owner_user_id=r.owner_user_id AND work_case_id=w.id;
 END LOOP;
 UPDATE public.work_cases SET requester_name=s.name,item_name=item,specification=spec,
  standardized_item_key=CASE WHEN item IS NOT NULL THEN lower(regexp_replace(btrim(item),'[[:space:]]+',' ','g'))
   ELSE standardized_item_key END,unit=coalesce(unit,p_record->>'unit'),updated_at=clock_timestamp() WHERE id=w.id;
 IF iid IS NOT NULL AND q.extraction_status='CONFIRMED' AND q.unit_price IS NOT NULL AND q.unit IS NOT NULL THEN
  INSERT INTO public.price_history(owner_user_id,supplier_item_id,quote_version_id,source_mail_id,unit_price,unit,currency,
   received_at,effective_from,valid_until,conditions_jsonb,evidence_jsonb)
  VALUES(r.owner_user_id,iid,q.id,m.id,q.unit_price,q.unit,q.currency,m.sent_or_received_at,q.effective_from,q.valid_until,
   q.pricing_terms_jsonb,q.evidence_jsonb);
  UPDATE public.supplier_items SET latest_reference_quote_id=q.id WHERE id=iid AND owner_user_id=r.owner_user_id;
 END IF;
 INSERT INTO public.workflow_logs(owner_user_id,work_case_id,agent_run_id,source_mail_id,event_type,actor_type,actor_id,
  input_revision,payload_jsonb) VALUES(r.owner_user_id,w.id,r.id,m.id,'QUOTE_STORED','AGENT','INFO_EXTRACTOR',r.input_revision,
  jsonb_build_object('quote_id',q.id,'version_no',q.version_no));
 RETURN jsonb_build_object('kind','STORED','quote_id',q.id,'record',p_record);
END $$;

CREATE FUNCTION public.complete_case_agent_extraction(p_job uuid,p_token uuid,p_revision integer,p_batch uuid,
 p_output jsonb,p_quote_ids uuid[],p_model text,p_prompt_version text,p_agent_output jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.agent_runs%ROWTYPE; w public.work_cases%ROWTYPE; step_ text;
BEGIN
 r:=internal.lock_case_agent_job(p_job,p_token,p_revision);
 SELECT * INTO STRICT w FROM public.work_cases WHERE id=r.work_case_id AND owner_user_id=r.owner_user_id;
 IF p_batch IS DISTINCT FROM r.review_batch_id OR p_output->>'case_id' IS DISTINCT FROM w.id::text
  OR (p_output->>'input_revision')::integer IS DISTINCT FROM r.input_revision
  OR p_output->>'status' IS NULL OR p_output->>'status' NOT IN ('EXTRACTION_READY','NEEDS_INFO','ON_HOLD')
  OR p_quote_ids IS NULL OR array_position(p_quote_ids,NULL) IS NOT NULL OR cardinality(p_quote_ids)>100
  OR nullif(p_model,'') IS NULL OR nullif(p_prompt_version,'') IS NULL
  OR jsonb_typeof(p_agent_output) IS DISTINCT FROM 'object'
  OR p_agent_output->>'next_action' IS DISTINCT FROM (CASE WHEN p_output->>'status'='ON_HOLD' THEN 'HOLD' ELSE 'STORED' END)
  OR nullif(p_agent_output->>'reason','') IS NULL THEN
  RAISE EXCEPTION 'invalid completion' USING ERRCODE='22023'; END IF;
 IF p_output->>'status'='ON_HOLD' THEN
  IF cardinality(p_quote_ids)<>0 THEN RAISE EXCEPTION 'hold quote references' USING ERRCODE='22023'; END IF;
  step_:='ON_HOLD';
 ELSE
  IF cardinality(p_quote_ids)=0 OR EXISTS(SELECT FROM unnest(p_quote_ids) x WHERE NOT EXISTS(
   SELECT FROM public.quote_versions q WHERE q.id=x AND q.owner_user_id=r.owner_user_id AND q.work_case_id=w.id
    AND q.source_mail_id=r.source_mail_id AND q.input_revision=r.input_revision)) THEN
   RAISE EXCEPTION 'unconfirmed quote references' USING ERRCODE='23514'; END IF;
  step_:=CASE WHEN nullif(w.item_name,'') IS NULL THEN 'NEEDS_INFO' ELSE 'CANDIDATES' END;
 END IF;
 PERFORM public.set_review_step(r.owner_user_id,w.id,r.input_revision,r.review_batch_id,step_);
 UPDATE public.agent_runs SET status='COMPLETED',finished_at=clock_timestamp(),model_id=p_model,prompt_version=p_prompt_version,
  output_jsonb=p_agent_output||p_output||jsonb_build_object('quote_version_ids',to_jsonb(p_quote_ids),'source_quote_ids',to_jsonb(p_quote_ids),
   'processing_step',step_),input_snapshot_jsonb=input_snapshot_jsonb||'{"job_status":"DONE"}'::jsonb WHERE id=r.id;
 INSERT INTO public.workflow_logs(owner_user_id,work_case_id,agent_run_id,source_mail_id,event_type,actor_type,actor_id,
  input_revision,payload_jsonb) VALUES(r.owner_user_id,w.id,r.id,r.source_mail_id,'EXTRACTION_STORED','AGENT','INFO_EXTRACTOR',
  r.input_revision,jsonb_build_object('quote_version_ids',to_jsonb(p_quote_ids),'processing_step',step_));
 RETURN jsonb_build_object('kind','STORED','quote_version_ids',to_jsonb(p_quote_ids));
END $$;

CREATE FUNCTION public.case_agent_store_health() RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN PERFORM internal.service_only(); RETURN jsonb_build_object('contract_version',1,'ready',true); END $$;
REVOKE ALL ON FUNCTION internal.lock_case_agent_job(uuid,uuid,integer), internal.agent_exact_number(jsonb),
 public.begin_case_agent_run(uuid,uuid,integer,text,text,jsonb),public.store_case_agent_quote(uuid,uuid,integer,jsonb),
 public.complete_case_agent_extraction(uuid,uuid,integer,uuid,jsonb,uuid[],text,text,jsonb),public.case_agent_store_health()
 FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION internal.lock_case_agent_job(uuid,uuid,integer),internal.agent_exact_number(jsonb),
 public.begin_case_agent_run(uuid,uuid,integer,text,text,jsonb),public.store_case_agent_quote(uuid,uuid,integer,jsonb),
 public.complete_case_agent_extraction(uuid,uuid,integer,uuid,jsonb,uuid[],text,text,jsonb),public.case_agent_store_health() TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;

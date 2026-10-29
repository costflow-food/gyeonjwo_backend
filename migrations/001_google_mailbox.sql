-- REVIEW/APPLY WITH THE DB OWNER. Not automatically executed by the API.
-- Observed public schema: gyeonjwo-dev, 2026-10-28. No credentials are embedded.
-- Private credential/request/job tables; existing public business tables are reused.
begin;
create schema if not exists gyeonjwo_private;
revoke all on schema gyeonjwo_private from public, anon, authenticated;

create table if not exists gyeonjwo_private.google_connections (
 mailbox_ref uuid primary key default gen_random_uuid(),
 user_id uuid not null unique references auth.users(id) on delete cascade,
 google_sub text not null unique,
 email text not null unique check(email=lower(email)),
 public_state jsonb not null,
 scopes jsonb not null check(jsonb_typeof(scopes)='array'),
 encrypted_refresh_token text,
 connection_revision bigint not null default 1,
 updated_at timestamptz not null default clock_timestamp()
);
create table if not exists gyeonjwo_private.google_requests (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references auth.users(id) on delete cascade,
 request_key text not null check(length(request_key) between 1 and 256),
 body_hash text not null check(body_hash ~ '^[a-f0-9]{64}$'),
 mailbox_ref uuid not null,
 status text not null check(status in ('IN_PROGRESS','RETRYABLE','COMPLETE')),
 first_response jsonb,
 created_at timestamptz not null default clock_timestamp(),
 updated_at timestamptz not null default clock_timestamp(),
 unique(user_id,request_key)
);
create unique index if not exists gj_one_connection_request
 on gyeonjwo_private.google_requests(user_id) where status='IN_PROGRESS';
create table if not exists gyeonjwo_private.google_sync_jobs (
 id uuid primary key default gen_random_uuid(),
 mailbox_ref uuid not null unique references gyeonjwo_private.google_connections(mailbox_ref) on delete cascade,
 user_id uuid not null references auth.users(id) on delete cascade,
 connection_revision bigint not null,
 request_version bigint not null default 1,
 claimed_version bigint,
 status text not null default 'PENDING' check(status in ('PENDING','RUNNING','IDLE','BACKOFF')),
 next_attempt_at timestamptz not null default clock_timestamp(),
 claim_token uuid, lease_expires_at timestamptz,
 last_history_id text check(last_history_id ~ '^[0-9]{1,30}$'),
 last_error_code text,
 attempts integer not null default 0
);
create table if not exists gyeonjwo_private.google_push_events (
 subscription text not null, message_id text not null,
 mailbox_ref uuid not null references gyeonjwo_private.google_connections(mailbox_ref) on delete cascade,
 history_id text not null check(history_id ~ '^[0-9]{1,30}$'),
 created_at timestamptz not null default clock_timestamp(),
 primary key(subscription,message_id)
);
create table if not exists gyeonjwo_private.google_ingested_messages (
 mailbox_ref uuid not null references gyeonjwo_private.google_connections(mailbox_ref) on delete cascade,
 message_id text not null, source_mail_id uuid,
 deleted boolean not null default false,
 primary key(mailbox_ref,message_id)
);
-- Durable handoff to the extraction/review orchestrator. A stored job is not a completed analysis.
create table if not exists gyeonjwo_private.google_review_events (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null, case_id uuid not null, source_mail_id uuid not null unique,
 status text not null default 'PENDING', created_at timestamptz not null default clock_timestamp()
);
alter table gyeonjwo_private.google_connections enable row level security;
alter table gyeonjwo_private.google_requests enable row level security;
alter table gyeonjwo_private.google_sync_jobs enable row level security;
alter table gyeonjwo_private.google_push_events enable row level security;
alter table gyeonjwo_private.google_ingested_messages enable row level security;
alter table gyeonjwo_private.google_review_events enable row level security;
revoke all on all tables in schema gyeonjwo_private from public, anon, authenticated;

-- Fail/roll back rather than silently adopting a different business schema.
do $$ begin
 if to_regclass('public.mail_messages') is null or to_regclass('public.work_cases') is null
    or to_regclass('public.mail_sync_states') is null then
  raise exception 'BUSINESS_SCHEMA_REQUIRED';
 end if;
end $$;
create unique index if not exists gj_google_mail_identity
 on public.mail_messages(owner_user_id,mailbox_ref,provider_message_id) where provider='GMAIL';
create unique index if not exists gj_google_sync_identity on public.mail_sync_states(owner_user_id,mailbox_ref);

create or replace function public.claim_google_connection_request(p_user_id uuid,p_key text,p_body_hash text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare r gyeonjwo_private.google_requests; m uuid;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_user_id::text,20261028));
 if length(btrim(p_key))=0 or length(p_key)>256 or p_body_hash !~ '^[a-f0-9]{64}$' then raise exception 'INVALID_REQUEST'; end if;
 select * into r from gyeonjwo_private.google_requests where user_id=p_user_id and request_key=p_key for update;
 if found then
  if r.body_hash<>p_body_hash then return jsonb_build_object('kind','CONFLICT'); end if;
  if r.status='COMPLETE' then return jsonb_build_object('kind','REPLAY','response',r.first_response); end if;
  if r.status='IN_PROGRESS' then return jsonb_build_object('kind','IN_PROGRESS'); end if;
 end if;
 if exists(select 1 from gyeonjwo_private.google_requests where user_id=p_user_id and status='IN_PROGRESS') then
  return jsonb_build_object('kind','IN_PROGRESS');
 end if;
 if r.id is null then
  select mailbox_ref into m from gyeonjwo_private.google_connections where user_id=p_user_id;
  insert into gyeonjwo_private.google_requests(user_id,request_key,body_hash,mailbox_ref,status)
   values(p_user_id,p_key,p_body_hash,coalesce(m,gen_random_uuid()),'IN_PROGRESS') returning * into r;
 else
  update gyeonjwo_private.google_requests set status='IN_PROGRESS',updated_at=clock_timestamp() where id=r.id;
 end if;
 return jsonb_build_object('kind','ACQUIRED','request_id',r.id,'mailbox_ref',r.mailbox_ref);
end $$;

create or replace function public.retry_google_connection_request(p_request_id uuid,p_user_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
begin
 perform pg_advisory_xact_lock(hashtextextended(p_user_id::text,20261028));
 update gyeonjwo_private.google_requests set status='RETRYABLE',updated_at=clock_timestamp()
  where id=p_request_id and user_id=p_user_id and status='IN_PROGRESS';
 return found;
end $$;

create or replace function public.finish_google_connection_request(p_request_id uuid,p_user_id uuid,p_response jsonb,p_connection jsonb)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare r gyeonjwo_private.google_requests; c gyeonjwo_private.google_connections; pub jsonb; rev bigint;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_user_id::text,20261028));
 select * into r from gyeonjwo_private.google_requests where id=p_request_id and user_id=p_user_id for update;
 if not found then return false; end if;
 if r.status='COMPLETE' then return r.first_response=p_response; end if;
 if r.status<>'IN_PROGRESS' or p_response is null or not(p_response ? 'body') or not(p_response ? 'status') then return false; end if;
 if p_connection is not null then
  pub=p_connection->'public';
  if (p_connection->>'mailbox_ref')::uuid<>r.mailbox_ref or pub->>'mailbox_ref'<>r.mailbox_ref::text
     or pub->>'email'<>p_connection->>'email' or nullif(p_connection->>'google_sub','') is null
     or pub->>'status' not in ('CONNECTED','PARTIAL','NEEDS_RECONNECT','FAILED')
     or jsonb_typeof(p_connection->'scopes')<>'array'
     or ((pub#>>'{capabilities,read}')::boolean is distinct from ((p_connection->'scopes') ? 'https://www.googleapis.com/auth/gmail.readonly'))
     or ((pub#>>'{capabilities,send}')::boolean is distinct from ((p_connection->'scopes') ? 'https://www.googleapis.com/auth/gmail.send')) then
   raise exception 'INVALID_CONNECTION';
  end if;
  if pub->>'status' in ('CONNECTED','PARTIAL') and nullif(p_connection->>'encrypted_refresh_token','') is null then raise exception 'CIPHERTEXT_REQUIRED'; end if;
  select * into c from gyeonjwo_private.google_connections where user_id=p_user_id for update;
  if found and c.google_sub<>p_connection->>'google_sub' then raise exception 'GOOGLE_ACCOUNT_CHANGED'; end if;
  rev=coalesce(c.connection_revision,0)+1;
  insert into gyeonjwo_private.google_connections(mailbox_ref,user_id,google_sub,email,public_state,scopes,encrypted_refresh_token,connection_revision)
   values(r.mailbox_ref,p_user_id,p_connection->>'google_sub',lower(p_connection->>'email'),pub,p_connection->'scopes',p_connection->>'encrypted_refresh_token',rev)
   on conflict(user_id) do update set google_sub=excluded.google_sub,email=excluded.email,public_state=excluded.public_state,
    scopes=excluded.scopes,encrypted_refresh_token=excluded.encrypted_refresh_token,connection_revision=rev,updated_at=clock_timestamp();
  -- Cancel the previous connection's lease, preserving only committed history.
  update gyeonjwo_private.google_sync_jobs set connection_revision=rev,status='IDLE',claim_token=null,lease_expires_at=null where mailbox_ref=r.mailbox_ref;
  if (p_connection->>'sync_requested')::boolean is true and (pub#>>'{capabilities,read}')::boolean is true
     and pub->>'status' in ('CONNECTED','PARTIAL') then
   insert into public.mail_sync_states(owner_user_id,mailbox_ref,sync_cursor_jsonb)
    values(p_user_id,r.mailbox_ref::text,'{}') on conflict(owner_user_id,mailbox_ref) do nothing;
   insert into gyeonjwo_private.google_sync_jobs(mailbox_ref,user_id,connection_revision)
    values(r.mailbox_ref,p_user_id,rev) on conflict(mailbox_ref) do update set
     connection_revision=rev,status='PENDING',request_version=google_sync_jobs.request_version+1,next_attempt_at=clock_timestamp();
  end if;
 end if;
 update gyeonjwo_private.google_requests set status='COMPLETE',first_response=p_response,updated_at=clock_timestamp() where id=r.id;
 return true;
end $$;

create or replace function public.get_google_mailbox_connection(p_user_id uuid)
returns jsonb language sql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
 select jsonb_build_object('mailbox_ref',mailbox_ref,'user_id',user_id,'google_sub',google_sub,'email',email,
  'public',public_state,'scopes',scopes,'encrypted_refresh_token',encrypted_refresh_token,'connection_revision',connection_revision)
 from gyeonjwo_private.google_connections where user_id=p_user_id
$$;

create or replace function public.mark_google_mailbox_reconnect(p_mailbox_ref uuid,p_connection_revision bigint)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
begin
 update gyeonjwo_private.google_connections set public_state=jsonb_set(jsonb_set(public_state,'{status}','"NEEDS_RECONNECT"'),'{watch}','{"started":false,"expiration":null}'),
  encrypted_refresh_token=null,connection_revision=connection_revision+1,updated_at=clock_timestamp()
  where mailbox_ref=p_mailbox_ref and connection_revision=p_connection_revision;
 return found;
end $$;

create or replace function public.rotate_connected_google_token(p_mailbox_ref uuid,p_connection_revision bigint,p_previous_ciphertext text,p_ciphertext text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
begin
 if nullif(p_ciphertext,'') is null then return false; end if;
 update gyeonjwo_private.google_connections set encrypted_refresh_token=p_ciphertext,updated_at=clock_timestamp()
 where mailbox_ref=p_mailbox_ref and connection_revision=p_connection_revision and encrypted_refresh_token=p_previous_ciphertext;
 return found;
end $$;

create or replace function public.enqueue_connected_gmail_notification(p_subscription text,p_message_id text,p_email text,p_history_id text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare c gyeonjwo_private.google_connections;
begin
 if nullif(p_subscription,'') is null or nullif(p_message_id,'') is null or p_history_id !~ '^[0-9]{1,30}$' then return false; end if;
 select * into c from gyeonjwo_private.google_connections where email=lower(p_email) for share;
 if not found or c.public_state->>'status' not in ('CONNECTED','PARTIAL') or (c.public_state#>>'{capabilities,read}')::boolean is not true then return false; end if;
 insert into gyeonjwo_private.google_push_events(subscription,message_id,mailbox_ref,history_id)
  values(p_subscription,p_message_id,c.mailbox_ref,p_history_id) on conflict do nothing;
 if not found then return true; end if;
 insert into gyeonjwo_private.google_sync_jobs(mailbox_ref,user_id,connection_revision)
 values(c.mailbox_ref,c.user_id,c.connection_revision) on conflict(mailbox_ref) do update set
  request_version=google_sync_jobs.request_version+1,
  status=case when google_sync_jobs.status='RUNNING' then 'RUNNING' else 'PENDING' end,
  next_attempt_at=clock_timestamp();
 return true;
end $$;

create or replace function public.schedule_connected_gmail_sync()
returns integer language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare n integer;
begin
 insert into gyeonjwo_private.google_sync_jobs(mailbox_ref,user_id,connection_revision)
 select mailbox_ref,user_id,connection_revision from gyeonjwo_private.google_connections
 where public_state->>'status' in ('CONNECTED','PARTIAL') and (public_state#>>'{capabilities,read}')::boolean is true and encrypted_refresh_token is not null
 on conflict(mailbox_ref) do update set status='PENDING',request_version=google_sync_jobs.request_version+1
 where google_sync_jobs.status='IDLE' and google_sync_jobs.next_attempt_at<=clock_timestamp();
 get diagnostics n=row_count; return n;
end $$;

create or replace function public.claim_connected_gmail_sync()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare j gyeonjwo_private.google_sync_jobs;
begin
 select q.* into j from gyeonjwo_private.google_sync_jobs q join gyeonjwo_private.google_connections c using(mailbox_ref)
 where c.connection_revision=q.connection_revision and c.public_state->>'status' in ('CONNECTED','PARTIAL')
 and (c.public_state#>>'{capabilities,read}')::boolean is true and c.encrypted_refresh_token is not null
 and ((q.status in ('PENDING','BACKOFF') and q.next_attempt_at<=clock_timestamp()) or (q.status='RUNNING' and q.lease_expires_at<clock_timestamp()))
 order by q.next_attempt_at,q.id for update of q skip locked limit 1;
 if not found then return null; end if;
 update gyeonjwo_private.google_sync_jobs set status='RUNNING',claim_token=gen_random_uuid(),lease_expires_at=clock_timestamp()+interval '120 seconds',
  claimed_version=request_version,attempts=attempts+1 where id=j.id returning * into j;
 return jsonb_build_object('job_id',j.id,'claim_token',j.claim_token,'mailbox_ref',j.mailbox_ref,'user_id',j.user_id,
  'connection_revision',j.connection_revision,'last_history_id',j.last_history_id);
end $$;

create or replace function gyeonjwo_private.require_google_sync(p_job_id uuid,p_claim_token uuid,p_revision bigint)
returns gyeonjwo_private.google_sync_jobs language plpgsql set search_path=pg_catalog,public,gyeonjwo_private as $$
declare j gyeonjwo_private.google_sync_jobs; c gyeonjwo_private.google_connections;
begin
 select * into j from gyeonjwo_private.google_sync_jobs where id=p_job_id;
 -- Exclusive from the outset: finish updates the watch on this row. Upgrading a
 -- shared lock after acquiring the job would deadlock with a concurrent push.
 select * into c from gyeonjwo_private.google_connections where mailbox_ref=j.mailbox_ref for update;
 if not found or c.connection_revision<>p_revision or c.public_state->>'status' not in ('CONNECTED','PARTIAL')
 or (c.public_state#>>'{capabilities,read}')::boolean is not true then raise exception 'SYNC_OWNERSHIP_LOST'; end if;
 select * into j from gyeonjwo_private.google_sync_jobs where id=p_job_id for update;
 if j.status<>'RUNNING' or j.claim_token is distinct from p_claim_token or j.lease_expires_at<=clock_timestamp()
 or j.connection_revision<>p_revision then raise exception 'SYNC_OWNERSHIP_LOST'; end if;
 return j;
end $$;

create or replace function public.heartbeat_connected_gmail_sync(p_job_id uuid,p_claim_token uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
begin
 update gyeonjwo_private.google_sync_jobs set lease_expires_at=clock_timestamp()+interval '120 seconds'
 where id=p_job_id and claim_token=p_claim_token and status='RUNNING' and lease_expires_at>clock_timestamp();
 return found;
end $$;

create or replace function public.finish_connected_gmail_sync(p_job_id uuid,p_claim_token uuid,p_connection_revision bigint,p_history_id text,p_watch jsonb)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare j gyeonjwo_private.google_sync_jobs;
begin
 j=gyeonjwo_private.require_google_sync(p_job_id,p_claim_token,p_connection_revision);
 if p_history_id !~ '^[0-9]{1,30}$' or (j.last_history_id is not null and p_history_id::numeric<j.last_history_id::numeric) then raise exception 'INVALID_CURSOR'; end if;
 update public.mail_sync_states set sync_cursor_jsonb=sync_cursor_jsonb||jsonb_build_object('gmail_history_id',p_history_id),last_synced_at=clock_timestamp(),
  next_sync_at=clock_timestamp()+interval '90 seconds',last_error_jsonb=null where owner_user_id=j.user_id and mailbox_ref=j.mailbox_ref::text;
 update gyeonjwo_private.google_connections set public_state=jsonb_set(public_state,'{watch}',p_watch) where mailbox_ref=j.mailbox_ref;
 update gyeonjwo_private.google_sync_jobs set last_history_id=p_history_id,
  status=case when request_version>claimed_version then 'PENDING' else 'IDLE' end,
  next_attempt_at=case when request_version>claimed_version then clock_timestamp() else clock_timestamp()+interval '90 seconds' end,
  claim_token=null,lease_expires_at=null,last_error_code=null,attempts=0 where id=j.id;
 return true;
end $$;

create or replace function public.fail_connected_gmail_sync(p_job_id uuid,p_claim_token uuid,p_code text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
begin
 update gyeonjwo_private.google_sync_jobs set status='BACKOFF',last_error_code='GMAIL_SYNC_FAILED',
  next_attempt_at=clock_timestamp()+make_interval(secs=>least(300,30*power(2,least(attempts,3)))::integer),claim_token=null,lease_expires_at=null
 where id=p_job_id and claim_token=p_claim_token and status='RUNNING';
 return found;
end $$;

create or replace function public.connected_gmail_message_exists(p_mailbox_ref uuid,p_message_id text)
returns boolean language sql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
 select exists(select 1 from gyeonjwo_private.google_ingested_messages where mailbox_ref=p_mailbox_ref and message_id=p_message_id)
 or exists(select 1 from public.mail_messages m join gyeonjwo_private.google_connections c on m.owner_user_id=c.user_id and m.mailbox_ref=c.mailbox_ref::text
  where c.mailbox_ref=p_mailbox_ref and m.provider='GMAIL' and m.provider_message_id=p_message_id)
$$;

create or replace function public.find_connected_gmail_reply_candidates(p_mailbox_ref uuid,p_message jsonb)
returns jsonb language sql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
 select coalesce(jsonb_agg(distinct jsonb_build_object('case_id',m.work_case_id,'same_participants',true,
  'matched_reference',m.normalized_jsonb->>'message_id_header')),'[]'::jsonb)
 from public.mail_messages m join gyeonjwo_private.google_connections c on c.user_id=m.owner_user_id and m.mailbox_ref=c.mailbox_ref::text
 join public.work_cases w on w.id=m.work_case_id and w.owner_user_id=c.user_id and w.mailbox_ref=c.mailbox_ref::text
 where c.mailbox_ref=p_mailbox_ref and m.direction='OUTBOUND' and m.normalized_jsonb->>'message_id_header' is not null
 and (m.normalized_jsonb->>'message_id_header') in
  (select (regexp_matches(coalesce(p_message->>'in_reply_to','')||' '||coalesce(p_message->>'references',''),'<[^<>\s]+>','g'))[1])
 and exists(select 1 from jsonb_array_elements_text(p_message->'sender') s(address)
  join jsonb_array_elements_text(m.recipients_jsonb) r(address) on lower(s.address)=lower(r.address))
$$;

create or replace function public.ingest_connected_gmail_message(p_job_id uuid,p_claim_token uuid,p_connection_revision bigint,p_message jsonb,p_classification jsonb,p_link jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
declare j gyeonjwo_private.google_sync_jobs; mid uuid; cid uuid; w public.work_cases; candidates jsonb; outcome text; classification text; link text;
begin
 j=gyeonjwo_private.require_google_sync(p_job_id,p_claim_token,p_connection_revision);
 if nullif(p_message->>'gmail_message_id','') is null then raise exception 'INVALID_MESSAGE'; end if;
 if public.connected_gmail_message_exists(j.mailbox_ref,p_message->>'gmail_message_id') then return '{"outcome":"DUPLICATE"}'; end if;
 insert into gyeonjwo_private.google_ingested_messages(mailbox_ref,message_id,deleted)
 values(j.mailbox_ref,p_message->>'gmail_message_id',coalesce((p_message->>'deleted')::boolean,false)) on conflict do nothing;
 if not found then return '{"outcome":"DUPLICATE"}'; end if;
 if (p_message->>'deleted')::boolean is true then return '{"outcome":"DELETED"}'; end if;
 classification=p_classification->>'status'; link=p_link->>'kind';
 if link='EXISTING_CASE' then
  cid=(p_link->>'case_id')::uuid;
  candidates=public.find_connected_gmail_reply_candidates(j.mailbox_ref,p_message);
  if (select count(distinct value->>'case_id') from jsonb_array_elements(candidates))<>1
     or not exists(select 1 from jsonb_array_elements(candidates) where value->>'case_id'=cid::text and value->>'matched_reference'=p_link->>'matched_reference') then raise exception 'LINK_EVIDENCE_CHANGED'; end if;
  select * into w from public.work_cases where id=cid and owner_user_id=j.user_id and mailbox_ref=j.mailbox_ref::text for update;
  if not found then raise exception 'LINK_SCOPE_MISMATCH'; end if;
  classification='BUSINESS';
 elsif classification not in ('BUSINESS','NON_BUSINESS','UNCERTAIN') or classification is null then raise exception 'INVALID_CLASSIFICATION';
 end if;
 insert into public.mail_messages(owner_user_id,mailbox_ref,provider_message_id,provider_thread_id,work_case_id,direction,subject,body_text,sender,
  recipients_jsonb,sent_or_received_at,attachments_jsonb,mail_classification,classification_source,classification_reason,normalized_jsonb)
 values(j.user_id,j.mailbox_ref::text,p_message->>'gmail_message_id',p_message->>'gmail_thread_id',cid,'INBOUND',coalesce(p_message->>'subject',''),
  coalesce(p_message->>'body_text',''),coalesce(p_message#>>'{sender,0}',''),coalesce(p_message->'recipients','[]'::jsonb),
  (p_message->>'received_at')::timestamptz,coalesce(p_message->'attachments','[]'::jsonb),classification,
  case when cid is not null then 'REPLY_EVIDENCE' else 'CLASSIFICATION_NODE' end,p_classification->>'reason',
  jsonb_build_object('message_id_header',p_message->>'message_id_header','in_reply_to',p_message->>'in_reply_to','references',p_message->>'references','sender_addresses',p_message->'sender'))
 returning id into mid;
 if cid is not null then
  if w.archived_at is not null or w.decision='ACCEPT_INCREASE' then outcome='ARCHIVED_REPLY';
  else
   insert into gyeonjwo_private.google_review_events(user_id,case_id,source_mail_id) values(j.user_id,cid,mid);
   outcome='LINKED_REPLY';
  end if;
 elsif link='AMBIGUOUS' or classification='UNCERTAIN' then outcome='INTAKE';
 elsif classification='NON_BUSINESS' then outcome='NON_BUSINESS';
 elsif link='NEW_MESSAGE' then
  insert into public.work_cases(owner_user_id,initial_mail_id,mailbox_ref,mandatory_conditions_jsonb,policy_jsonb,status,processing_step)
   values(j.user_id,mid,j.mailbox_ref::text,'{}','{}','Waiting','PENDING_START') returning id into cid;
  update public.mail_messages set work_case_id=cid where id=mid;
  outcome='NEW_CASE';
 else raise exception 'INVALID_LINK'; end if;
 update public.mail_messages set link_status=case when cid is null then 'UNMATCHED' else 'LINKED' end where id=mid;
 update gyeonjwo_private.google_ingested_messages set source_mail_id=mid where mailbox_ref=j.mailbox_ref and message_id=p_message->>'gmail_message_id';
 return jsonb_build_object('outcome',outcome,'mail_id',mid,'case_id',cid);
end $$;

-- Readiness must work with zero connected users too.
create or replace function public.connected_google_mailbox_health()
returns text language plpgsql security definer set search_path=pg_catalog,public,gyeonjwo_private as $$
begin
 perform 1 from gyeonjwo_private.google_connections limit 0;
 perform 1 from gyeonjwo_private.google_requests limit 0;
 perform 1 from gyeonjwo_private.google_sync_jobs limit 0;
 perform 1 from public.mail_messages limit 0;
 perform 1 from public.work_cases limit 0;
 return 'google-mailbox-v1';
end $$;

-- An RPC with a user_id argument is not safe to expose to browser JWT roles.
do $$ declare f record; begin
 for f in select p.oid::regprocedure signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.proname in (
 'claim_google_connection_request','retry_google_connection_request','finish_google_connection_request','get_google_mailbox_connection',
 'mark_google_mailbox_reconnect','rotate_connected_google_token','enqueue_connected_gmail_notification','schedule_connected_gmail_sync',
 'claim_connected_gmail_sync','heartbeat_connected_gmail_sync','finish_connected_gmail_sync','fail_connected_gmail_sync',
 'connected_gmail_message_exists','find_connected_gmail_reply_candidates','ingest_connected_gmail_message','connected_google_mailbox_health') loop
  execute format('revoke all on function %s from public, anon, authenticated',f.signature);
  execute format('grant execute on function %s to service_role',f.signature);
 end loop;
end $$;
revoke all on all functions in schema gyeonjwo_private from public,anon,authenticated;
notify pgrst,'reload schema';
commit;

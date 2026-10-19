-- CostFlow 2026-10-19: reviewed reconstruction from the referenced ChatGPT discussion.
-- Original uploaded v2.0 document was unavailable. No seed data is included.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public, extensions;
LOCK TABLE public.suppliers IN ACCESS EXCLUSIVE MODE;
DO $guard$
BEGIN
 IF current_database() <> 'postgres' OR current_user <> 'postgres' THEN RAISE EXCEPTION 'Unexpected database/role'; END IF;
 IF EXISTS (SELECT 1 FROM public.suppliers) THEN RAISE EXCEPTION 'suppliers is no longer empty; stop for data migration'; END IF;
 IF (SELECT array_agg(column_name::text ORDER BY ordinal_position) FROM information_schema.columns WHERE table_schema='public' AND table_name='suppliers') IS DISTINCT FROM ARRAY['supplier_id','supplier_name','relationship','certifications','experience_comment','source_ref'] THEN RAISE EXCEPTION 'Unexpected suppliers definition'; END IF;
 IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=ANY(ARRAY['items','contracts','purchase_plans','price_references','cost_terms','work_items','request_fields','review_results'])) THEN RAISE EXCEPTION 'Target tables already exist'; END IF;
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname IN ('public','storage')) THEN RAISE EXCEPTION 'Policies changed; re-audit'; END IF;
 IF EXISTS (SELECT 1 FROM storage.buckets WHERE id='costflow-files') THEN RAISE EXCEPTION 'Bucket already exists; re-audit'; END IF;
END $guard$;
-- Prevent automatic API grants for objects created by this migration owner.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated, service_role;
-- PUBLIC function defaults are global and cannot be removed by a schema-local default ACL.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated, service_role;


-- 1. 계약 기간 중복 검사에 사용할 확장
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS btree_gist
WITH SCHEMA extensions;

-- 2. 빈 문자열, 공백 문자열 방지
CREATE DOMAIN public.nonblank_text AS TEXT
CHECK (
    VALUE IS NULL OR VALUE ~ '[^[:space:]]'
);

-- 3. ENUM 타입
CREATE TYPE public.work_status AS ENUM (
    'ON_HOLD', 'APPROVED', 'REJECTED'
);

CREATE TYPE public.request_field_name AS ENUM (
    'supplier_name',
    'item_name',
    'specification',
    'origin',
    'grade',
    'processing',
    'old_unit_price',
    'new_unit_price',
    'unit',
    'currency',
    'effective_date',
    'reason',
    'requested_quantity',
    'requested_delivery_date',
    'transport_amount',
    'other_logistics_amount',
    'cost_basis',
    'cost_payer',
    'cost_included_in_price'
);

CREATE TYPE public.price_rule AS ENUM (
    'FIXED', 'CHANGEABLE'
);

CREATE TYPE public.reference_type AS ENUM (
    'PURCHASE_HISTORY', 'QUOTE'
);

CREATE TYPE public.cost_owner_type AS ENUM (
    'CONTRACT', 'REFERENCE'
);

CREATE TYPE public.cost_type AS ENUM (
    'TRANSPORT', 'OTHER_LOGISTICS'
);

CREATE TYPE public.cost_basis AS ENUM (
    'TOTAL', 'PER_UNIT', 'UNKNOWN'
);

CREATE TYPE public.cost_payer AS ENUM (
    'BUYER', 'SUPPLIER', 'UNKNOWN'
);

CREATE TYPE public.price_inclusion AS ENUM (
    'TRUE', 'FALSE', 'UNKNOWN'
);

-- 4. 거래처: 5개 컬럼
-- Existing empty suppliers is altered in place; no table is dropped.
ALTER TABLE public.suppliers
 DROP COLUMN relationship,
 ALTER COLUMN supplier_id TYPE public.nonblank_text USING supplier_id::public.nonblank_text,
 ALTER COLUMN supplier_name TYPE public.nonblank_text USING supplier_name::public.nonblank_text,
 ALTER COLUMN certifications TYPE public.nonblank_text USING certifications::public.nonblank_text,
 ALTER COLUMN experience_comment TYPE public.nonblank_text USING experience_comment::public.nonblank_text,
 ALTER COLUMN source_ref TYPE public.nonblank_text USING source_ref::public.nonblank_text,
 ADD CONSTRAINT chk_supplier_comment_date CHECK (experience_comment IS NULL OR experience_comment ~ '^\[[0-9]{4}-[0-9]{2}-[0-9]{2}\]');

-- 5. 품목: 6개 컬럼
CREATE TABLE public.items (
    item_id public.nonblank_text PRIMARY KEY,
    item_name public.nonblank_text NOT NULL,
    specification public.nonblank_text NOT NULL,
    origin public.nonblank_text,
    grade public.nonblank_text,
    processing public.nonblank_text NOT NULL,

    CONSTRAINT uq_item_spec UNIQUE NULLS NOT DISTINCT (
        item_name, specification, origin,
        grade, processing
    )
);

-- 6. 계약: 9개 컬럼
CREATE TABLE public.contracts (
    contract_id public.nonblank_text PRIMARY KEY,

    supplier_id public.nonblank_text NOT NULL
        REFERENCES public.suppliers(supplier_id),

    item_id public.nonblank_text NOT NULL
        REFERENCES public.items(item_id),

    valid_from DATE NOT NULL,
    valid_to DATE NOT NULL,

    price_rule public.price_rule NOT NULL,
    unit_price NUMERIC(14,2) NOT NULL,

    adjustment_clause public.nonblank_text NOT NULL,
    source_ref public.nonblank_text NOT NULL,

    CONSTRAINT chk_contract_dates
        CHECK (valid_to >= valid_from),

    CONSTRAINT chk_contract_price
        CHECK (unit_price >= 0),

    CONSTRAINT ex_contract_period EXCLUDE USING gist (
        supplier_id WITH =,
        item_id WITH =,
        daterange(valid_from, valid_to, '[]') WITH &&
    )
);

-- 7. 구매계획: 5개 컬럼
CREATE TABLE public.purchase_plans (
    plan_id public.nonblank_text PRIMARY KEY,

    contract_id public.nonblank_text NOT NULL
        REFERENCES public.contracts(contract_id),

    delivery_date DATE NOT NULL,
    quantity NUMERIC(14,3) NOT NULL,
    source_ref public.nonblank_text NOT NULL,

    CONSTRAINT chk_plan_quantity
        CHECK (quantity > 0)
);

-- 8. 가격 근거: 13개 컬럼
CREATE TABLE public.price_references (
    reference_id public.nonblank_text PRIMARY KEY,

    reference_type public.reference_type NOT NULL,

    supplier_id public.nonblank_text NOT NULL
        REFERENCES public.suppliers(supplier_id),

    item_id public.nonblank_text NOT NULL
        REFERENCES public.items(item_id),

    reference_date DATE NOT NULL,
    valid_until DATE,

    unit_price NUMERIC(14,2),
    quantity NUMERIC(14,3),
    moq NUMERIC(14,3),
    lead_days INTEGER,

    delivery_terms public.nonblank_text,
    trade_stage public.nonblank_text NOT NULL,
    source_ref public.nonblank_text NOT NULL,

    CONSTRAINT chk_reference_dates CHECK (
        valid_until IS NULL
        OR valid_until >= reference_date
    ),

    CONSTRAINT chk_reference_price CHECK (
        unit_price IS NULL OR unit_price >= 0
    ),

    CONSTRAINT chk_reference_quantity CHECK (
        quantity IS NULL OR quantity > 0
    ),

    CONSTRAINT chk_reference_moq CHECK (
        moq IS NULL OR moq > 0
    ),

    CONSTRAINT chk_reference_lead CHECK (
        lead_days IS NULL OR lead_days >= 0
    ),

    CONSTRAINT chk_purchase_history_quantity CHECK (
        reference_type <> 'PURCHASE_HISTORY'
        OR quantity IS NOT NULL
    )
);

-- 9. 비용 조건: 11개 컬럼
CREATE TABLE public.cost_terms (
    owner_type public.cost_owner_type NOT NULL,
    owner_id public.nonblank_text NOT NULL,
    cost_type public.cost_type NOT NULL,

    amount NUMERIC(14,2),
    basis public.cost_basis NOT NULL,
    payer public.cost_payer NOT NULL,
    included_in_price public.price_inclusion NOT NULL,

    coverage_from DATE,
    coverage_to DATE,
    coverage_quantity NUMERIC(14,3),
    source_ref public.nonblank_text NOT NULL,

    PRIMARY KEY (owner_type, owner_id, cost_type),

    CONSTRAINT chk_cost_amount CHECK (
        amount IS NULL OR amount >= 0
    ),

    CONSTRAINT chk_cost_dates CHECK (
        coverage_from IS NULL
        OR coverage_to IS NULL
        OR coverage_to >= coverage_from
    ),

    CONSTRAINT chk_cost_quantity CHECK (
        coverage_quantity IS NULL
        OR coverage_quantity > 0
    )
);

-- 10. 업무 큐: 3개 컬럼
CREATE TABLE public.work_items (
    work_id public.nonblank_text PRIMARY KEY,

    classification_reason public.nonblank_text,

    status public.work_status NOT NULL
        DEFAULT 'ON_HOLD',

    CONSTRAINT chk_work_id_format CHECK (
        work_id ~ '^W-[0-9]{8}-[0-9]{3}$'
    )
);

-- 11. 추출 정보: 5개 컬럼
CREATE TABLE public.request_fields (
    work_id public.nonblank_text NOT NULL
        REFERENCES public.work_items(work_id)
        ON DELETE CASCADE,

    line_no INTEGER NOT NULL,

    field_name public.request_field_name NOT NULL,
    raw_value public.nonblank_text,
    source_location public.nonblank_text NOT NULL,

    PRIMARY KEY (work_id, line_no, field_name),

    CONSTRAINT chk_field_line_no
        CHECK (line_no >= 1)
);

-- 12. 최종 결정: 4개 컬럼
CREATE TABLE public.review_results (
    work_id public.nonblank_text NOT NULL
        REFERENCES public.work_items(work_id)
        ON DELETE CASCADE,

    line_no INTEGER NOT NULL,

    reference_id public.nonblank_text
        REFERENCES public.price_references(reference_id),

    summary public.nonblank_text NOT NULL,

    PRIMARY KEY (work_id, line_no),

    CONSTRAINT chk_result_line_no
        CHECK (line_no >= 1)
);

-- 13. 성능 인덱스
CREATE INDEX idx_contract_supplier_item
ON public.contracts(supplier_id, item_id);

CREATE INDEX idx_plan_delivery
ON public.purchase_plans(delivery_date);

CREATE INDEX idx_reference_supplier_item
ON public.price_references(
    supplier_id, item_id, reference_type
);

CREATE INDEX idx_fields_work
ON public.request_fields(work_id);

-- 14. RLS: 처음부터 공개 접근 차단
ALTER TABLE public.suppliers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.purchase_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cost_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.work_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.request_fields ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.review_results ENABLE ROW LEVEL SECURITY;

-- 브라우저용 역할의 접근 제거
REVOKE ALL ON TABLE
    public.suppliers,
    public.items,
    public.contracts,
    public.purchase_plans,
    public.price_references,
    public.cost_terms,
    public.work_items,
    public.request_fields,
    public.review_results
FROM PUBLIC, anon, authenticated;


-- Finite values and dates: numeric NaN must not satisfy a non-negative business amount.
ALTER TABLE public.contracts ADD CONSTRAINT contract_finite CHECK (isfinite(valid_from) AND isfinite(valid_to) AND unit_price::text NOT IN ('NaN','Infinity','-Infinity'));
ALTER TABLE public.purchase_plans ADD CONSTRAINT plan_finite CHECK (isfinite(delivery_date) AND quantity::text NOT IN ('NaN','Infinity','-Infinity'));
ALTER TABLE public.price_references ADD CONSTRAINT reference_finite CHECK (isfinite(reference_date) AND (valid_until IS NULL OR isfinite(valid_until)) AND (unit_price IS NULL OR unit_price::text NOT IN ('NaN','Infinity','-Infinity')) AND (quantity IS NULL OR quantity::text NOT IN ('NaN','Infinity','-Infinity')) AND (moq IS NULL OR moq::text NOT IN ('NaN','Infinity','-Infinity')));
ALTER TABLE public.cost_terms ADD CONSTRAINT cost_finite CHECK ((coverage_from IS NULL OR isfinite(coverage_from)) AND (coverage_to IS NULL OR isfinite(coverage_to)) AND (amount IS NULL OR amount::text NOT IN ('NaN','Infinity','-Infinity')) AND (coverage_quantity IS NULL OR coverage_quantity::text NOT IN ('NaN','Infinity','-Infinity')));

CREATE FUNCTION public.purchase_plans_guard_fn() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE a date; b date;
BEGIN
 SELECT valid_from,valid_to INTO a,b FROM public.contracts WHERE contract_id=NEW.contract_id FOR SHARE;
 IF NOT FOUND OR NEW.delivery_date NOT BETWEEN a AND b THEN RAISE EXCEPTION 'Delivery must fall within the contract period' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER purchase_plans_guard BEFORE INSERT OR UPDATE ON public.purchase_plans FOR EACH ROW EXECUTE FUNCTION public.purchase_plans_guard_fn();

CREATE FUNCTION public.cost_terms_guard_fn() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
 IF NEW.owner_type='CONTRACT' THEN
  PERFORM 1 FROM public.contracts WHERE contract_id=NEW.owner_id FOR KEY SHARE;
 ELSE
  PERFORM 1 FROM public.price_references WHERE reference_id=NEW.owner_id FOR KEY SHARE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION 'Cost owner does not exist' USING ERRCODE='23503'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER cost_terms_guard BEFORE INSERT OR UPDATE ON public.cost_terms FOR EACH ROW EXECUTE FUNCTION public.cost_terms_guard_fn();

CREATE FUNCTION public.costflow_master_guard_fn() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE id text; kind public.cost_owner_type;
BEGIN
 IF TG_TABLE_NAME='contracts' THEN
  id:=OLD.contract_id; kind:='CONTRACT';
  IF TG_OP='UPDATE' THEN
   IF NEW.contract_id IS DISTINCT FROM OLD.contract_id THEN RAISE EXCEPTION 'Contract ID is immutable' USING ERRCODE='23514'; END IF;
   IF EXISTS(SELECT 1 FROM public.purchase_plans WHERE contract_id=id AND delivery_date NOT BETWEEN NEW.valid_from AND NEW.valid_to) THEN RAISE EXCEPTION 'Contract period excludes an existing plan' USING ERRCODE='23514'; END IF;
  END IF;
 ELSE
  id:=OLD.reference_id; kind:='REFERENCE';
  IF TG_OP='UPDATE' THEN
   IF NEW.reference_id IS DISTINCT FROM OLD.reference_id THEN RAISE EXCEPTION 'Reference ID is immutable' USING ERRCODE='23514'; END IF;
   IF NEW.reference_type<>'QUOTE' AND EXISTS(SELECT 1 FROM public.review_results WHERE reference_id=id) THEN RAISE EXCEPTION 'Selected quote type is immutable' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 IF TG_OP='DELETE' THEN
  IF EXISTS(SELECT 1 FROM public.cost_terms WHERE owner_type=kind AND owner_id=id) THEN RAISE EXCEPTION 'Cost owner is referenced' USING ERRCODE='23503'; END IF;
  RETURN OLD;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER costflow_master_guard BEFORE UPDATE OR DELETE ON public.contracts FOR EACH ROW EXECUTE FUNCTION public.costflow_master_guard_fn();
CREATE TRIGGER costflow_master_guard BEFORE UPDATE OR DELETE ON public.price_references FOR EACH ROW EXECUTE FUNCTION public.costflow_master_guard_fn();

CREATE FUNCTION public.work_items_guard_fn() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'ON_HOLD' THEN RAISE EXCEPTION 'New work must start ON_HOLD' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF OLD.status<>'ON_HOLD' THEN RAISE EXCEPTION 'Closed work is immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN
  IF EXISTS(SELECT 1 FROM public.review_results WHERE work_id=OLD.work_id) THEN RAISE EXCEPTION 'Decided work cannot be deleted' USING ERRCODE='23514'; END IF;
  RETURN OLD;
 END IF;
 IF NEW.work_id IS DISTINCT FROM OLD.work_id THEN RAISE EXCEPTION 'Work ID is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.status='REJECTED' AND EXISTS(SELECT 1 FROM public.review_results WHERE work_id=NEW.work_id) THEN RAISE EXCEPTION 'Partly decided work cannot be rejected' USING ERRCODE='23514'; END IF;
 IF NEW.status='APPROVED' THEN
  IF NOT EXISTS(SELECT 1 FROM public.request_fields WHERE work_id=NEW.work_id) OR EXISTS(
   SELECT 1 FROM public.request_fields f WHERE f.work_id=NEW.work_id GROUP BY f.line_no
   HAVING count(*)<>19 OR NOT EXISTS(SELECT 1 FROM public.review_results r WHERE r.work_id=NEW.work_id AND r.line_no=f.line_no)
  ) THEN RAISE EXCEPTION 'All complete lines require decisions before approval' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER work_items_guard BEFORE INSERT OR UPDATE OR DELETE ON public.work_items FOR EACH ROW EXECUTE FUNCTION public.work_items_guard_fn();

CREATE FUNCTION public.request_fields_guard_fn() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE id text; st public.work_status;
BEGIN
 IF TG_OP='DELETE' THEN id:=OLD.work_id; ELSE id:=NEW.work_id; END IF;
 IF TG_OP='UPDATE' AND (NEW.work_id,NEW.line_no,NEW.field_name) IS DISTINCT FROM (OLD.work_id,OLD.line_no,OLD.field_name) THEN RAISE EXCEPTION 'Field identity is immutable' USING ERRCODE='23514'; END IF;
 SELECT status INTO st FROM public.work_items WHERE work_id=id FOR UPDATE;
 IF NOT FOUND OR st<>'ON_HOLD' OR EXISTS(SELECT 1 FROM public.review_results WHERE work_id=id) THEN RAISE EXCEPTION 'Extraction is frozen after a decision' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER request_fields_guard BEFORE INSERT OR UPDATE OR DELETE ON public.request_fields FOR EACH ROW EXECUTE FUNCTION public.request_fields_guard_fn();

CREATE FUNCTION public.review_results_guard_fn() RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE st public.work_status;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Decisions are immutable' USING ERRCODE='23514'; END IF;
 SELECT status INTO st FROM public.work_items WHERE work_id=NEW.work_id FOR UPDATE;
 IF NOT FOUND OR st<>'ON_HOLD' THEN RAISE EXCEPTION 'Only held work can be decided' USING ERRCODE='23514'; END IF;
 IF (SELECT count(*) FROM public.request_fields WHERE work_id=NEW.work_id AND line_no=NEW.line_no)<>19 THEN RAISE EXCEPTION 'Decision requires 19 extracted fields for its line' USING ERRCODE='23514'; END IF;
 IF NEW.reference_id IS NOT NULL THEN
  PERFORM 1 FROM public.price_references WHERE reference_id=NEW.reference_id AND reference_type='QUOTE' FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Only QUOTE can be selected' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER review_results_guard BEFORE INSERT OR UPDATE OR DELETE ON public.review_results FOR EACH ROW EXECUTE FUNCTION public.review_results_guard_fn();

CREATE SEQUENCE public.costflow_work_id_seq AS bigint NO CYCLE;
CREATE FUNCTION public.issue_work_id() RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE day_text text; floor_value bigint; number_value bigint; prior_value bigint; result text;
BEGIN
 -- A single transaction-scoped lock serializes the daily counter across callers.
 PERFORM pg_catalog.pg_advisory_xact_lock(20261019,61001);
 day_text:=pg_catalog.to_char(pg_catalog.clock_timestamp() AT TIME ZONE 'Asia/Seoul','YYYYMMDD');
 floor_value:=day_text::bigint*1000;
 SELECT last_value INTO prior_value FROM public.costflow_work_id_seq;
 IF prior_value<floor_value THEN PERFORM pg_catalog.setval('public.costflow_work_id_seq'::regclass,floor_value,true);
 ELSIF prior_value>=floor_value+1000 THEN RAISE EXCEPTION 'Clock moved backwards; ID issuance stopped'; END IF;
 LOOP
  number_value:=pg_catalog.nextval('public.costflow_work_id_seq'::regclass);
  IF number_value>floor_value+999 THEN RAISE EXCEPTION 'Daily work ID capacity exceeded (999)' USING ERRCODE='22003'; END IF;
  result:='W-'||day_text||'-'||pg_catalog.lpad((number_value-floor_value)::text,3,'0');
  EXIT WHEN NOT EXISTS(SELECT 1 FROM public.work_items WHERE work_id=result);
 END LOOP;
 RETURN result;
END $$;

CREATE FUNCTION public.save_request_fields(p_work_id text,p_rows jsonb) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE st public.work_status; n integer;
BEGIN
 SELECT status INTO st FROM public.work_items WHERE work_id=p_work_id FOR UPDATE;
 IF NOT FOUND OR st<>'ON_HOLD' OR EXISTS(SELECT 1 FROM public.review_results WHERE work_id=p_work_id) THEN RAISE EXCEPTION 'Extraction requires undecided held work' USING ERRCODE='23514'; END IF;
 IF pg_catalog.jsonb_typeof(p_rows) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'p_rows must be an array' USING ERRCODE='22023'; END IF;
 n:=pg_catalog.jsonb_array_length(p_rows);
 IF n=0 OR n>19000 OR n%19<>0 THEN RAISE EXCEPTION 'Each line requires exactly 19 fields, up to 1000 lines' USING ERRCODE='22023'; END IF;
 IF EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(p_rows) x WHERE pg_catalog.jsonb_typeof(x)<>'object' OR x->>'line_no' IS NULL OR x->>'field_name' IS NULL OR x->>'source_location' IS NULL) THEN RAISE EXCEPTION 'Malformed extracted field' USING ERRCODE='22023'; END IF;
 DELETE FROM public.request_fields WHERE work_id=p_work_id;
 INSERT INTO public.request_fields(work_id,line_no,field_name,raw_value,source_location)
 SELECT p_work_id,(x->>'line_no')::integer,(x->>'field_name')::public.request_field_name,
 CASE WHEN (x->>'raw_value') ~ '[^[:space:]]' THEN x->>'raw_value' ELSE NULL END,x->>'source_location'
 FROM pg_catalog.jsonb_array_elements(p_rows) x;
 IF EXISTS(SELECT 1 FROM public.request_fields WHERE work_id=p_work_id GROUP BY line_no HAVING count(*)<>19) THEN RAISE EXCEPTION 'Every line requires every defined field exactly once' USING ERRCODE='23514'; END IF;
 RETURN n;
END $$;

CREATE FUNCTION public.record_decision(p_work_id text,p_line_no integer,p_reference_id text,p_summary text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE st public.work_status;
BEGIN
 SELECT status INTO st FROM public.work_items WHERE work_id=p_work_id FOR UPDATE;
 IF NOT FOUND OR st<>'ON_HOLD' THEN RAISE EXCEPTION 'Work is missing or already closed' USING ERRCODE='23514'; END IF;
 IF p_line_no IS NULL OR p_line_no<1 OR p_summary IS NULL OR p_summary !~ '[^[:space:]]' THEN RAISE EXCEPTION 'Valid line and nonblank summary are required' USING ERRCODE='22023'; END IF;
 INSERT INTO public.review_results(work_id,line_no,reference_id,summary) VALUES(p_work_id,p_line_no,nullif(pg_catalog.btrim(p_reference_id),''),p_summary);
 -- Keep multi-line work open until every extracted line has its own immutable decision.
 IF NOT EXISTS(SELECT 1 FROM public.request_fields f WHERE f.work_id=p_work_id AND NOT EXISTS(SELECT 1 FROM public.review_results r WHERE r.work_id=f.work_id AND r.line_no=f.line_no)) THEN
  UPDATE public.work_items SET status='APPROVED' WHERE work_id=p_work_id;
 END IF;
END $$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC,anon,authenticated,service_role;
DO $acl$
DECLARE t text; cols text;
BEGIN
 FOREACH t IN ARRAY ARRAY['suppliers','items','contracts','purchase_plans','price_references','cost_terms','work_items','request_fields','review_results'] LOOP
  EXECUTE pg_catalog.format('REVOKE ALL ON TABLE public.%I FROM PUBLIC,anon,authenticated,service_role',t);
  SELECT pg_catalog.string_agg(pg_catalog.quote_ident(column_name),',') INTO cols FROM information_schema.columns WHERE table_schema='public' AND table_name=t;
  EXECUTE pg_catalog.format('REVOKE SELECT(%s),INSERT(%s),UPDATE(%s),REFERENCES(%s) ON TABLE public.%I FROM PUBLIC,anon,authenticated,service_role',cols,cols,cols,cols,t);
 END LOOP;
END $acl$;
GRANT SELECT ON public.suppliers,public.items,public.contracts,public.purchase_plans,public.price_references,public.cost_terms,public.work_items,public.request_fields,public.review_results TO service_role;
GRANT INSERT ON public.work_items TO service_role;
GRANT UPDATE(classification_reason,status) ON public.work_items TO service_role;
REVOKE ALL ON SEQUENCE public.costflow_work_id_seq FROM PUBLIC,anon,authenticated,service_role;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.issue_work_id(),public.save_request_fields(text,jsonb),public.record_decision(text,integer,text,text) TO service_role;
INSERT INTO storage.buckets(id,name,public,file_size_limit) VALUES('costflow-files','costflow-files',false,52428800);
NOTIFY pgrst,'reload schema';


COMMIT;


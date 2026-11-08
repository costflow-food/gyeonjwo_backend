-- 합성 시나리오 전용. 자동 실행하지 않음. 실제 DB 컬럼/제약 조회: 2026-11-08.
-- 아래 owner, 구매자 메일, 업체별 수신 가능한 테스트 주소 4개를 DB 담당자가 지정한다.
-- 기존 깐마늘 자료와 섞이지 않도록 전용 테스트 소유자를 권장한다.
begin;
set local role service_role;
do $seed$
declare
  v_owner uuid := null;
  v_buyer_email text := null;
  -- 순서: 기존 / 대안 A / 제외 B / 대안 C(운송비 미확인) / 대안 D(600kg)
  v_emails text[] := array[null, null, null, null, null]::text[];
  v_names text[] := array['시연마늘농산','시연대서유통','시연미확인농산','시연깐마늘식품','시연알마늘'];
  v_roles text[] := array['incumbent','alternative_a','excluded_b','alternative_c','alternative_d'];
  v_seed text := 'it-garlic-daeseo-2026-11';
  v_spec text := '국산 대서 상품, 20kg 포장';
  v_supplier uuid;
  v_item uuid;
  v_purchase uuid;
  v_price numeric;
  v_quantity numeric;
  v_i integer;
begin
  if v_owner is null or nullif(btrim(v_buyer_email),'') is null then
    raise exception '테스트 owner와 구매자 메일을 먼저 지정하세요.';
  end if;
  if cardinality(v_emails) <> 5 or v_emails[3] is not null then
    raise exception '주소 배열은 5칸이며 제외 B의 주소는 NULL이어야 합니다.';
  end if;
  if (select count(distinct lower(btrim(e))) from unnest(v_emails) e where nullif(btrim(e),'') is not null) <> 4
     or exists(select 1 from unnest(v_emails) e where e is not null and
       (e !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' or e like '%.invalid'
        or lower(btrim(e)) = lower(btrim(v_buyer_email)))) then
    raise exception '구매자와 다른 테스트 메일 4개를 서로 겹치지 않게 지정하세요.';
  end if;
  if exists(select 1 from public.suppliers where owner_user_id=v_owner and
     (evidence_jsonb->>'seed'=v_seed or name=any(v_names)
      or lower(contact_email)=any(array(select lower(btrim(e)) from unnest(v_emails) e)))) then
    raise exception '기존 시드, 업체 이름 또는 주소가 겹칩니다. 기존 자료를 덮어쓰지 않습니다.';
  end if;
  if exists(select 1 from public.supplier_items where owner_user_id=v_owner and item_name like '%마늘%')
     or exists(select 1 from public.company_items where owner_user_id=v_owner and item_name like '%마늘%') then
    raise exception '이 소유자에게 기존 마늘 자료가 있습니다. 분리된 테스트 계정을 사용하세요.';
  end if;
  for v_i in 1..5 loop
    insert into public.suppliers(owner_user_id,name,contact_email,source_type,evidence_jsonb)
    values(v_owner,v_names[v_i],lower(btrim(v_emails[v_i])),'REGISTERED',
           jsonb_build_object('seed',v_seed,'role',v_roles[v_i],'synthetic',true)) returning id into v_supplier;
    insert into public.supplier_items(owner_user_id,supplier_id,item_name,specification,unit,weight_per_box,
                                     certifications_jsonb,origin,notes_evidence_jsonb)
    values(v_owner,v_supplier,'깐마늘',v_spec,'kg',20,
           case when v_i=3 then '[]'::jsonb else '["HACCP"]'::jsonb end,'국산',
           jsonb_build_object('seed',v_seed,'synthetic',true)) returning id into v_item;
    if v_i in (1,2,5) then
      v_price := case v_i when 1 then 9400 when 2 then 9700 else 9500 end;
      v_quantity := case v_i when 1 then 1000 when 2 then 300 else 200 end;
      insert into public.purchase_history(owner_user_id,supplier_item_id,supplier_id,transaction_date,
                                         quantity,unit,unit_price,total_amount,currency,evidence_jsonb)
      values(v_owner,v_item,v_supplier,'2026-10-15',v_quantity,'kg',v_price,v_quantity*v_price,'KRW',
             jsonb_build_object('seed',v_seed,'synthetic',true)) returning id into v_purchase;
      update public.supplier_items set last_purchase_id=v_purchase where id=v_item and owner_user_id=v_owner;
    end if;
  end loop;
  -- standardized_item_key는 기존 트리거가 부여한다. 정책/발송 설정/운영 레코드를 직접 쓰지 않는다.
  insert into public.company_items(owner_user_id,item_name,specification,unit,mandatory_conditions_jsonb)
  values(v_owner,'깐마늘',v_spec,'kg',jsonb_build_object('certifications',jsonb_build_array('HACCP'),'specification',v_spec));
  if (select count(distinct standardized_item_key) from public.supplier_items
      where owner_user_id=v_owner and notes_evidence_jsonb->>'seed'=v_seed) <> 1 then
    raise exception '시드 품목 키가 하나로 묶이지 않았습니다.';
  end if;
  if not exists(select 1 from public.company_items c join public.supplier_items i
     on c.owner_user_id=i.owner_user_id and c.standardized_item_key=i.standardized_item_key
     where c.owner_user_id=v_owner and i.notes_evidence_jsonb->>'seed'=v_seed) then
    raise exception '회사 품목과 공급처 품목 키가 다릅니다.';
  end if;
end
$seed$;
commit;

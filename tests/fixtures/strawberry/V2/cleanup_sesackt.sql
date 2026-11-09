-- 냉동딸기 V2 sesackt 몫 시드 삭제 (owner 한정)
-- 실행 전 __OWNER_USER_ID__를 sesackt 계정의 사용자 UUID(auth.users.id)로 바꿔 넣는다. 실제 UUID는 저장소에 두지 않는다.
begin;
update supplier_items set last_purchase_id = null where owner_user_id = '__OWNER_USER_ID__' and notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
delete from purchase_history where owner_user_id = '__OWNER_USER_ID__' and evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
delete from supplier_items where owner_user_id = '__OWNER_USER_ID__' and notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
delete from suppliers where owner_user_id = '__OWNER_USER_ID__' and evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
commit;

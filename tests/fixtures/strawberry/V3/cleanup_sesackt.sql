-- 냉동딸기 V3 sesackt 몫 시드 삭제 (owner 한정)
begin;
update supplier_items set last_purchase_id = null where owner_user_id = 'bee2b4e4-5a37-47da-9624-97809702c03f' and notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V3';
delete from purchase_history where owner_user_id = 'bee2b4e4-5a37-47da-9624-97809702c03f' and evidence_jsonb->>'synthetic' = 'SCN-STR-V3';
delete from supplier_items where owner_user_id = 'bee2b4e4-5a37-47da-9624-97809702c03f' and notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V3';
delete from suppliers where owner_user_id = 'bee2b4e4-5a37-47da-9624-97809702c03f' and evidence_jsonb->>'synthetic' = 'SCN-STR-V3';
commit;

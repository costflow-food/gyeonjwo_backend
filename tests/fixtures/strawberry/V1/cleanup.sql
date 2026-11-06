-- 냉동딸기 V1 합성 시드 삭제
begin;
update supplier_items set last_purchase_id = null where notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V1';
delete from purchase_history where evidence_jsonb->>'synthetic' = 'SCN-STR-V1';
delete from supplier_items where notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V1';
delete from suppliers where evidence_jsonb->>'synthetic' = 'SCN-STR-V1';
commit;

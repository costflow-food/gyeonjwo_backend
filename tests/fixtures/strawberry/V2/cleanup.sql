-- 냉동딸기 V2 합성 시드 삭제
begin;
update supplier_items set last_purchase_id = null where notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
delete from purchase_history where evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
delete from supplier_items where notes_evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
delete from suppliers where evidence_jsonb->>'synthetic' = 'SCN-STR-V2';
commit;

-- E2 업체 불일치: V1 시드의 한결냉동식품을 실제 발신 주소에 연결한다 (V1 seed 다음에 실행, owner 무관)
-- 되돌리기는 ../V1/cleanup.sql (합성 행째 삭제)
update suppliers set contact_email = 'hongmo9212@gmail.com'
where name = '한결냉동식품' and evidence_jsonb->>'synthetic' = 'SCN-STR-V1';

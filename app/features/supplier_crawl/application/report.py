"""시험 결과를 팀 공유용 마크다운으로 정리한다. 후보 값은 출처 근거와 함께 보여 준다."""
from app.features.supplier_crawl.domain.schemas import TrialReport

SOURCE_LABELS = {'PUBLIC_DATA': '공공데이터(품목제조보고·인허가)', 'HACCP': 'HACCP 업체 정보', 'HOMEPAGE': '업체 홈페이지'}


def _cell(value) -> str:
    return str(value or '-').replace('|', '/').replace('\n', ' ')


def to_markdown(report: TrialReport, candidate_limit: int = 30) -> str:
    lines = [f'# 크롤링 시험 결과 — {report.keyword}', '',
             f'- 수집 시각: {report.started_at:%Y-%m-%d %H:%M} ~ {report.finished_at:%H:%M} (UTC)',
             f'- 잠정 후보: {len(report.candidates)}곳 · 모든 값은 RFQ 회신·체크리스트로 확인 전 상태', '',
             '## 출처별 결과', '', '| 출처 | 실행 | 요청 | 실패 | 레코드 | 후보 | 소요(초) |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for s in report.sources:
        run = '실행' if s.enabled else '건너뜀: ' + _cell(s.skipped_reason)
        lines.append(f'| {SOURCE_LABELS[s.source_type]} | {run} | {s.requests} | {s.failed_requests} | {s.records} '
                     f'| {s.candidates} | {s.elapsed_seconds} |')
    lines += ['', '## 필드 채움률', '', '| 항목 | 채움 |', '| --- | --- |']
    lines += [f'| {name} | {rate} |' for name, rate in report.fill_rates.items()]
    lines += ['', '## 후보 샘플', '', '| 업체 | 출처 | 소재지 | 전화 | 이메일 | 취급 품목(근거) | HACCP |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    for c in report.candidates[:candidate_limit]:
        phones = ', '.join(x.value for x in c.contacts if x.contact_type == 'PHONE')
        emails = ', '.join(x.value for x in c.contacts if x.contact_type == 'EMAIL')
        items = '; '.join(f'{i.item_name}({i.evidence.source_type})' for i in c.handled_items[:3])
        if len(c.handled_items) > 3:
            items += f' 외 {len(c.handled_items) - 3}'
        haccp = ', '.join(_cell(x.item_type) for x in c.certifications)
        lines.append(f'| {_cell(c.name)} | {", ".join(c.source_types)} | {_cell(c.location)} | {_cell(phones)} '
                     f'| {_cell(emails)} | {_cell(items)} | {_cell(haccp)} |')
    if len(report.candidates) > candidate_limit:
        lines.append(f'\n외 {len(report.candidates) - candidate_limit}곳은 JSON 결과에 있습니다.')
    lines += ['', '## 발생한 오류·확인 사항', '']
    if report.issues:
        lines += ['| 출처 | 코드 | 내용 | 대상 |', '| --- | --- | --- | --- |']
        lines += [f'| {SOURCE_LABELS[i.source_type]} | {i.code} | {_cell(i.message)} | {_cell(i.target)} |'
                  for i in report.issues]
    else:
        lines.append('없음')
    lines += ['', '## 다음 구현 사항', '', '- (시험 결과를 보고 팀이 작성)', '']
    return '\n'.join(lines)

"""두 실행 결과를 시나리오별로 맞대어 비교한다. 검증자 켬·끔, 모델(Terra·Luna), 코드 수정 전·후 비교에 쓴다.

    python -m scripts.agent_eval.compare eval_results\\<A 폴더> eval_results\\<B 폴더>

시나리오마다 반복 회차의 통과 비율을 내고, A와 B에서 결과가 달라진 시나리오를 "B에서 좋아짐 / 나빠짐"으로 나눈다.
예: A = 검증자 끔(--no-verifier), B = 검증자 켬 → B에서 좋아짐 = 검증 덕분에 통과, 나빠짐 = 검증 때문에 실패.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from scripts.agent_eval.grade import grade_record
from scripts.agent_eval.scenarios import BY_ID, ROLE_CODES


def load(folder: str) -> tuple[dict, dict]:
    path = Path(folder)
    meta = json.loads((path / 'meta.json').read_text(encoding='utf-8')) if (path / 'meta.json').exists() else {}
    rates = defaultdict(lambda: [0, 0])          # 시나리오 → [PASS, PASS+FAIL]
    for line in (path / 'runs.jsonl').read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        g = grade_record(json.loads(line))
        if g['verdict'] in ('PASS', 'FAIL'):
            rates[g['id']][1] += 1
            rates[g['id']][0] += g['verdict'] == 'PASS'
    return meta, rates


def main(a: str, b: str):
    meta_a, ra = load(a)
    meta_b, rb = load(b)
    label = lambda m: json.dumps(m.get('options', {}), ensure_ascii=False)
    print(f'A {Path(a).name} {label(meta_a)}\nB {Path(b).name} {label(meta_b)}\n')
    print('| 역할 | A 통과율 | B 통과율 | B에서 좋아짐 | B에서 나빠짐 |\n| --- | --- | --- | --- | --- |')
    better, worse = [], []
    for role, code in ROLE_CODES.items():
        ids = sorted(set(ra) & set(rb), key=lambda i: (i[0], i))
        ids = [i for i in ids if BY_ID[i].role == role]
        if not ids:
            continue
        pa = sum(ra[i][0] for i in ids) / max(sum(ra[i][1] for i in ids), 1)
        pb = sum(rb[i][0] for i in ids) / max(sum(rb[i][1] for i in ids), 1)
        up = [i for i in ids if rb[i][0] / rb[i][1] > ra[i][0] / ra[i][1]]
        down = [i for i in ids if rb[i][0] / rb[i][1] < ra[i][0] / ra[i][1]]
        better += up
        worse += down
        print(f'| {code} {role} | {pa:.1%} | {pb:.1%} | {len(up)} | {len(down)} |')
    for title, rows in (('B에서 좋아진 시나리오', better), ('B에서 나빠진 시나리오', worse)):
        print(f'\n## {title} ({len(rows)}개)')
        for i in rows:
            print(f'- {i} {BY_ID[i].title}: A {ra[i][0]}/{ra[i][1]} → B {rb[i][0]}/{rb[i][1]}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])

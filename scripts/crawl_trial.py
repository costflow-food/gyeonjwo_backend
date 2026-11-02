"""KAN-98 크롤링 시험 수집. 결과는 logs/crawl_trial/ 아래 JSON·MD로 저장한다(logs/는 Git 제외).

python -m scripts.crawl_trial --keyword 딸기
python -m scripts.crawl_trial --keyword 딸기 --sources public,haccp --haccp-item-type 과·채가공품
python -m scripts.crawl_trial --keyword 딸기 --sources homepage --seeds seeds.txt

인증키는 .env에만 둔다(FOOD_SAFETY_API_KEY, DATA_GO_KR_SERVICE_KEY, NAVER_CLIENT_ID/SECRET).
출력·리포트에는 인증키가 남지 않는다. DB에는 저장하지 않는다.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app.core.config import ROOT
from app.features.supplier_crawl.application.report import to_markdown
from app.features.supplier_crawl.application.trial import SupplierCrawlTrial, TrialOptions
from app.features.supplier_crawl.infrastructure.config import CrawlSettings
from app.features.supplier_crawl.infrastructure.homepage import read_seed_file
from app.features.supplier_crawl.infrastructure.http import Fetcher

SOURCES = ('public', 'haccp', 'homepage')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--keyword', required=True, help='품목 검색어 (예: 딸기)')
    parser.add_argument('--sources', default=','.join(SOURCES), help='public,haccp,homepage 중 선택')
    parser.add_argument('--item-limit', type=int, default=30, help='품목제조보고 최대 행 수')
    parser.add_argument('--business-limit', type=int, default=10, help='연락처를 보강할 최대 업소 수')
    parser.add_argument('--haccp-pages', type=int, default=3, help='HACCP 목록을 읽을 쪽 수 (한 쪽 최대 100건)')
    parser.add_argument('--haccp-rows', type=int, default=100, help='한 쪽 결과 수 (서비스 한도 100, 넘으면 100으로 맞춤)')
    parser.add_argument('--haccp-item-type', help='HACCP 품목 유형으로 새 후보도 찾을 때 (예: 과·채가공품)')
    parser.add_argument('--site-limit', type=int, default=5, help='홈페이지를 찾을 최대 업체 수')
    parser.add_argument('--naver-query', help='네이버 지역 검색어 (지정할 때만 지역 검색 사용)')
    parser.add_argument('--seeds', type=Path, help='홈페이지 주소 목록 파일 (한 줄에 "주소" 또는 "주소,업체명")')
    parser.add_argument('--item-types', default='', help='품목유형 필터, 쉼표 구분 (예: 과·채가공품,기타가공품)')
    parser.add_argument('--include-ended', action='store_true', help='생산 종료 품목도 포함')
    parser.add_argument('--out', type=Path, default=ROOT / 'logs' / 'crawl_trial')
    args = parser.parse_args(argv)
    sources = tuple(s.strip() for s in args.sources.split(',') if s.strip())
    if not sources or any(s not in SOURCES for s in sources):
        parser.error('--sources는 public,haccp,homepage 중에서 고릅니다.')
    for name, upper in (('item_limit', 1000), ('business_limit', 50), ('haccp_pages', 200), ('haccp_rows', 1000),
                        ('site_limit', 50)):
        if not 1 <= getattr(args, name) <= upper:
            parser.error(f'--{name.replace("_", "-")}는 1~{upper} 사이입니다.')
    args.sources = sources
    return args


def main(argv=None, settings=None, transport=None):
    args = parse_args(argv)
    settings = settings or CrawlSettings()
    seeds = tuple(read_seed_file(args.seeds)) if args.seeds else ()
    options = TrialOptions(keyword=args.keyword.strip(), sources=args.sources, item_limit=args.item_limit,
                           business_limit=args.business_limit, haccp_pages=args.haccp_pages, haccp_rows=args.haccp_rows,
                           haccp_item_type=args.haccp_item_type, site_limit=args.site_limit,
                           naver_query=args.naver_query, seeds=seeds,
                           item_types=tuple(t.strip() for t in args.item_types.split(',') if t.strip()),
                           include_ended=args.include_ended)
    fetcher = Fetcher(settings, transport=transport)
    try:
        report = SupplierCrawlTrial(settings, fetcher).run(options)
    finally:
        fetcher.close()
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f'{datetime.now():%Y%m%d_%H%M%S}_{options.keyword}'
    payload = report.model_dump(mode='json')
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    for secret in settings.secrets():
        if secret in text:
            print('리포트에 인증키가 포함되어 저장하지 않았습니다.', file=sys.stderr)
            return 2
    stem.with_suffix('.json').write_text(text, encoding='utf-8')
    stem.with_suffix('.md').write_text(to_markdown(report), encoding='utf-8')
    summary = {'candidates': len(report.candidates), 'issues': len(report.issues),
               'sources': {s.source_type: ('ran' if s.enabled else 'skipped') for s in report.sources},
               'saved': [str(stem.with_suffix('.json')), str(stem.with_suffix('.md'))]}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

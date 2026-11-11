"""KAN-204: 운영 환경변수 이름이 코드가 읽는 이름과 맞는지 확인한다. 값은 읽어도 출력하지 않는다.

  python -m scripts.check_env_names                       # .env.example ↔ 코드 비교
  python -m scripts.check_env_names --env-file prod.env   # 운영 값(.env 또는 Lightsail environment JSON) 점검
  python -m scripts.check_env_names --env-file env.json --write-pruned out.json   # 읽지 않는 이름을 뺀 사본
  python -m unittest scripts.check_env_names              # 이 도구와 .env.example 회귀 점검

종료 코드: 0 = 맞음, 1 = 코드가 읽지 않는 이름이 있음(또는 .env.example 불일치), 2 = 입력을 읽지 못함.
앱을 import하지 않고 소스만 읽는다(배포 스크립트에서 의존성 없이 실행).
"""
import argparse
import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 코드가 아니라 실행기가 읽는 운영자 설정. uvicorn --proxy-headers 가 신뢰할 프록시 주소로 읽는다(Dockerfile 기본 127.0.0.1)
RUNTIME_READ = {'FORWARDED_ALLOW_IPS'}
# Python·uvicorn·OS·라이브러리가 읽을 수 있는 이름. 우리 코드에는 없어도 지우면 동작이 바뀔 수 있어 삭제 대상에서 뺀다
LIBRARY_NAMES = {
    'TZ', 'LANG', 'LANGUAGE', 'HOME', 'PATH', 'TMPDIR', 'WEB_CONCURRENCY',
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT'}
LIBRARY_PREFIXES = ('PYTHON', 'UVICORN_', 'LC_', 'LANGSMITH_', 'LANGCHAIN_', 'OPENAI_', 'OMP_', 'ORT_', 'OPENCV_')
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def settings_names(path: Path = ROOT / 'app/core/config.py') -> set[str]:
    """Settings 필드 중 env에서 읽는 이름. ClassVar 상수는 env를 읽지 않으므로 뺀다."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'Settings':
            for item in node.body:
                if (isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                        and item.target.id != 'model_config' and 'ClassVar' not in ast.unparse(item.annotation)):
                    names.add(item.target.id.upper())
    return names


def environ_reads(app_dir: Path = ROOT / 'app') -> set[str]:
    """app 코드가 os.environ.get('X')·os.getenv('X')·os.environ['X']로 직접 읽는 이름. 쓰기만 하는 이름은 뺀다."""
    names = set()
    for path in sorted(app_dir.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            key = None
            if isinstance(node, ast.Call) and node.args:
                func = ast.unparse(node.func)
                if func in ('os.environ.get', 'os.getenv', 'environ.get', 'getenv'):
                    key = node.args[0]
            elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                  and ast.unparse(node.value) in ('os.environ', 'environ')):
                key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value.upper())
    return names


def image_names(path: Path = ROOT / 'Dockerfile') -> set[str]:
    """Dockerfile ENV가 정하는 이름(이미지가 값을 준다). 운영자가 넣을 필요가 없다."""
    text = re.sub(r'\\\s*\n', ' ', path.read_text(encoding='utf-8'))
    names = set()
    for line in text.splitlines():
        if line.strip().upper().startswith('ENV '):
            names |= {match.upper() for match in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=', line.strip()[4:])}
    return names


def operator_names() -> set[str]:
    """운영자가 정하는 이름 = 코드·실행기가 읽는 이름 − 이미지가 정하는 이름. .env.example과 같아야 한다."""
    return (settings_names() | environ_reads() | RUNTIME_READ) - (image_names() - RUNTIME_READ)


def env_file_names(path: Path) -> list[str]:
    """.env 형식 또는 JSON 객체({이름: 값})에서 이름만 꺼낸다."""
    text = Path(path).read_text(encoding='utf-8')
    if text.lstrip().startswith('{'):
        return list(json.loads(text))
    names = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('export '):
            line = line[7:].lstrip()
        if line and not line.startswith('#') and '=' in line:
            key = line.split('=', 1)[0].strip()
            if NAME.match(key):
                names.append(key)
    return names


def is_library(name: str) -> bool:
    upper = name.upper()
    return upper in LIBRARY_NAMES or upper.startswith(LIBRARY_PREFIXES)


def classify(names) -> dict[str, list[str]]:
    """운영 이름을 나눈다. unread = 아무도 읽지 않음(삭제 대상), library = 라이브러리가 읽을 수 있음(유지·확인),
    image = 이미지가 정하는 값을 덮어씀(유지·확인), missing = 운영에 없어 기본값을 쓰는 운영자 설정."""
    wanted, image = operator_names(), image_names() - RUNTIME_READ
    given = {name.upper(): name for name in names}
    result = {'unread': [], 'library': [], 'image': [], 'missing': sorted(wanted - set(given))}
    for upper, name in sorted(given.items()):
        if upper in wanted:
            continue
        result['image' if upper in image else 'library' if is_library(name) else 'unread'].append(name)
    return result


def write_pruned(source: Path, target: Path, unread: list[str]) -> None:
    """읽지 않는 이름을 뺀 사본을 쓴다. 입력 형식(.env 또는 JSON)을 유지한다."""
    drop = {name.upper() for name in unread}
    text = Path(source).read_text(encoding='utf-8')
    if text.lstrip().startswith('{'):
        kept = {key: value for key, value in json.loads(text).items() if key.upper() not in drop}
        Path(target).write_text(json.dumps(kept), encoding='utf-8')
        return
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        key = (line[7:].lstrip() if line.startswith('export ') else line).split('=', 1)[0].strip()
        if not line or line.startswith('#') or '=' not in line or key.upper() not in drop:
            lines.append(raw)
    Path(target).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='운영 환경변수 이름 점검(값은 출력하지 않음)')
    parser.add_argument('--env-file', help='.env 또는 JSON 객체 파일. 없으면 .env.example을 코드와 비교한다')
    parser.add_argument('--json', action='store_true', help='결과를 JSON 한 줄로 출력')
    parser.add_argument('--write-pruned', metavar='OUT', help='읽지 않는 이름을 뺀 사본을 OUT에 쓴다')
    args = parser.parse_args(argv)
    try:
        names = env_file_names(Path(args.env_file) if args.env_file else ROOT / '.env.example')
        result = classify(names)
    except Exception as error:   # 값이 섞인 예외 문구를 내보내지 않는다
        print(f'check_env_names: 입력을 읽지 못했습니다 ({type(error).__name__})', file=sys.stderr)
        return 2
    if not args.env_file:
        # .env.example은 운영자 설정 이름과 정확히 같아야 한다
        extra = result['unread'] + result['library'] + result['image']
        result = {'unread': extra, 'library': [], 'image': [], 'missing': result['missing']}
        failed = bool(extra or result['missing'])
    else:
        failed = bool(result['unread'])
        if args.write_pruned:
            write_pruned(Path(args.env_file), Path(args.write_pruned), result['unread'])
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        labels = {'unread': '코드가 읽지 않는 이름(삭제 대상)', 'library': '라이브러리가 읽을 수 있는 이름(유지, 필요 여부 확인)',
                  'image': '이미지 기본값을 덮어쓰는 이름(유지, 의도 확인)',
                  'missing': '없는 이름(.env.example에 추가)' if not args.env_file else '운영에 없는 이름(코드 기본값 사용)'}
        for key, label in labels.items():
            if result[key]:
                print(f'{label} {len(result[key])}개: {", ".join(result[key])}')
        if args.env_file and 'FORWARDED_ALLOW_IPS' in result['missing']:
            print('주의: FORWARDED_ALLOW_IPS가 없으면 이미지 기본값 127.0.0.1만 신뢰한다. '
                  '프록시가 다른 주소면 X-Forwarded-Proto를 믿지 않아 HTTPS_REQUIRED가 난다')
        print('결과: ' + ('불일치' if failed else '일치'))
    return 1 if failed else 0


# 티켓(KAN-204)에 적힌, 운영에 남아 있던 이름. 다시 읽기 시작하면 이 점검이 알려 준다
TICKET_UNREAD = [
    'RFQ_MAIL_SENDING_ENABLED', 'RFQ_WORKFLOW_ENABLED', 'RFQ_METADATA_WORKER_ENABLED', 'RFQ_WORKFLOW_OWNER_IDS',
    'RFQ_METADATA_OWNER_IDS', 'WORKER_ENABLED', 'WORKER_POLL_SECONDS', 'WORKER_SYNC_SECONDS',
    'GOOGLE_MAILBOX_WORKER_ENABLED', 'GOOGLE_MAILBOX_POLL_SECONDS', 'CRAWL_DELAY_SECONDS',
    'CRAWL_MAX_PAGES_PER_SITE', 'CRAWL_USER_AGENT', 'HACCP_API_URL', 'LLM_REASONING_EFFORT_CANDIDATE',
    'LLM_MODEL', 'LLM_TIMEOUT_SECONDS']


class EnvNameTests(unittest.TestCase):
    def test_env_example_matches_code(self):
        example = {name.upper() for name in env_file_names(ROOT / '.env.example')}
        self.assertEqual(example, operator_names())

    def test_required_proxy_setting_is_documented(self):
        self.assertIn('FORWARDED_ALLOW_IPS', env_file_names(ROOT / '.env.example'))

    def test_ticket_names_are_unread(self):
        self.assertEqual(sorted(classify(TICKET_UNREAD)['unread']), sorted(TICKET_UNREAD))

    def test_class_constants_are_not_env(self):
        names = settings_names()
        self.assertNotIn('LLM_MODEL', names)
        self.assertNotIn('LLM_TIMEOUT_SECONDS', names)
        self.assertIn('SUPABASE_URL', names)

    def test_direct_reads_are_kept(self):
        reads = environ_reads()
        self.assertLessEqual({'LOG_LEVEL', 'PDF_WORKER_MEMORY_MB'}, reads)
        self.assertNotIn('OPENCV_IO_MAX_IMAGE_PIXELS', reads)   # 쓰기만 한다
        result = classify(['LOG_LEVEL', 'PDF_WORKER_MEMORY_MB', 'log_level'])
        self.assertEqual(result['unread'], [])

    def test_image_and_library_names_are_not_removed(self):
        result = classify(['APP_BUILD_REVISION', 'OCR_MODEL_DIR', 'TZ', 'PYTHONUNBUFFERED', 'LANGSMITH_ENDPOINT',
                           'HTTPS_PROXY', 'WORKER_ENABLED'])
        self.assertEqual(result['unread'], ['WORKER_ENABLED'])
        self.assertEqual(result['image'], ['APP_BUILD_REVISION', 'OCR_MODEL_DIR', 'PYTHONUNBUFFERED'])
        self.assertEqual(result['library'], ['HTTPS_PROXY', 'LANGSMITH_ENDPOINT', 'TZ'])

    def test_prune_keeps_values_and_never_prints_them(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'env.json', Path(folder) / 'out.json'
            env = {name: 'value-' + name for name in operator_names()} | {name: 'SECRET-VALUE' for name in TICKET_UNREAD}
            source.write_text(json.dumps(env), encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                self.assertEqual(main(['--env-file', str(source), '--write-pruned', str(target)]), 1)
            self.assertNotIn('SECRET-VALUE', out.getvalue())
            self.assertNotIn('value-', out.getvalue())
            kept = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(kept, {name: 'value-' + name for name in operator_names()})
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['--env-file', str(target), '--json']), 0)

    def test_dotenv_format_and_unreadable_input(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'prod.env', Path(folder) / 'out.env'
            source.write_text('# 주석\nSUPABASE_URL=https://x\nexport WORKER_ENABLED=true\n\nCORS_ORIGINS=["a=b"]\n',
                              encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                self.assertEqual(main(['--env-file', str(source), '--write-pruned', str(target)]), 1)
                self.assertEqual(main(['--env-file', str(Path(folder) / 'none')]), 2)
            self.assertEqual(target.read_text(encoding='utf-8'),
                             '# 주석\nSUPABASE_URL=https://x\n\nCORS_ORIGINS=["a=b"]\n')
            self.assertNotIn('https://x', out.getvalue())


if __name__ == '__main__':
    raise SystemExit(main())

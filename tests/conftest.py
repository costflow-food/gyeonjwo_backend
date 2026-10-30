"""Repository-owned offline mode: pytest --offline (no private bootstrap needed)."""
import json
import os
from pathlib import Path
import sys

import pytest


def pytest_addoption(parser):
    parser.addoption('--offline', action='store_true', help='Block network and real credential files; disable live-model opt-ins.')


def pytest_configure(config):
    if not config.getoption('--offline'):
        return
    for key in list(os.environ):
        if key.startswith(('LLM_', 'OPENAI_', 'KAN60_OPENAI_', 'ANTHROPIC_', 'LANGSMITH_',
                           'LANGCHAIN_', 'SUPABASE_', 'GMAIL_', 'RUN_LIVE')) or key.upper() in (
                               'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            os.environ.pop(key, None)
    counts = {'real_key_reads': 0, 'network_attempts': 0, 'evals_or_reports_reads': 0}
    config._agents_offline_counts = counts

    def audit(event, args):
        if event in ('socket.connect', 'socket.sendto', 'socket.getaddrinfo',
                     'socket.gethostbyname', 'socket.gethostbyaddr'):
            counts['network_attempts'] += 1
            raise RuntimeError('OFFLINE_NETWORK_FORBIDDEN')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            name = path.name
            if name == '.env' or name.startswith('.env.') and name != '.env.example':
                counts['real_key_reads'] += 1
                raise RuntimeError('REAL_KEY_FILE_FORBIDDEN')
            if {'evals', 'reports'} & set(path.parts):
                counts['evals_or_reports_reads'] += 1
                raise RuntimeError('TEST_ARTIFACT_DEPENDENCY_FORBIDDEN')

    sys.addaudithook(audit)


@pytest.hookimpl(optionalhook=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    counts = getattr(config, '_agents_offline_counts', None)
    if counts is not None:
        terminalreporter.write_line('OFFLINE_AUDIT=' + json.dumps(counts, sort_keys=True))

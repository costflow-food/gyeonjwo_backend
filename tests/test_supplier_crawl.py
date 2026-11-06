"""Offline fixtures only: no supplier list is shipped with the collector."""
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from app.features.supplier_crawl.application import tool as module
from app.features.supplier_crawl.infrastructure import google_search as search
from app.features.supplier_crawl.infrastructure.homepage_contacts import collect_homepage_contacts, extract_contacts

HTML = '''<div class="MjjYud"><a href="https://supplier.test/products"><h3>딸기 공급업체</h3></a>
<div class="VwiC3b">동결건조 딸기 안내</div></div>'''
CONTACTS = '<footer><a href="mailto:sales@supplier.test?subject=hello">문의</a><a href="tel:02-1234-5678">전화</a></footer>'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Unexpected external connection')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(search, '_search_page', lambda url: (HTML, url))


def test_query_homepage_contacts_and_provenance(monkeypatch):
    requests = []
    browser = Mock(return_value=(HTML, search.SEARCH_URL))
    monkeypatch.setattr(search, '_search_page', browser)
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=CONTACTS + '<a href="/contact">Contact</a>', headers={'content-type': 'text/html'})
    result = search.search_suppliers('동결건조 딸기 제조 업체', transport=httpx.MockTransport(respond))
    assert 'q=%EB%8F%99' in browser.call_args.args[0]
    assert requests == ['https://supplier.test/']
    item = result['suppliers'][0]
    assert result['status'] == 'OK' and result['fetched_at']
    assert item['search_title'] == '딸기 공급업체'
    assert item['search_result_url'] == 'https://supplier.test/products'
    assert item['homepage_url'] == item['contact_source_url'] == 'https://supplier.test/'
    assert item['emails'] == ['sales@supplier.test'] and item['phones'] == ['02-1234-5678']
    assert item['contact_status'] == 'FOUND'


def test_emails_phones_and_hidden_content():
    body = '''<a href="MAILTO:Sales%40supplier.test?subject=test">메일</a>
    <a href="tel:+82-2-1234-5678">대표전화</a>
    <p>sales@supplier.test</p><p>전화 031-123-4567</p><p>팩스 031-987-6543</p>
    <p>등록일 2026-11-05</p><p>사업자번호 123-45-67890</p>
    <script>secret@script.test</script><style>.x{email:style@style.test}</style>
    <div hidden>hidden@hidden.test</div><img src="image@2x.png">'''
    assert extract_contacts(body) == {'emails': ['sales@supplier.test'], 'phones': ['+82-2-1234-5678', '031-123-4567']}


def test_plain_text_contacts_and_legacy_encoding():
    body = '<p>이메일: info@supplier.test</p><p>Tel: +86 592 1234567</p>'
    encoded = ('<meta charset="euc-kr">' + body).encode('euc-kr')
    assert extract_contacts(encoded) == {'emails': ['info@supplier.test'], 'phones': ['+86 592 1234567']}


def test_phone_and_fax_on_same_line():
    assert extract_contacts('<p>Tel: 02-1234-5678 Fax: 02-9999-8888</p>')['phones'] == ['02-1234-5678']


def test_phone_does_not_merge_numbers_in_parentheses():
    body = '<p>Tel : 02. 575. 7289 ( 7288 , 7296 )</p><p>Fax : 02 - 575 - 1216</p>'
    assert extract_contacts(body)['phones'] == ['02. 575. 7289']


def test_bank_account_is_not_a_phone_and_labelled_plain_digits_are_read():
    body = '<p>Tel.</p><p>01094146161</p><p>은행 221-0119-6999-01</p><p>TEL. 01049431917</p>'
    assert extract_contacts(body)['phones'] == ['01094146161', '01049431917']


def test_copyright_year_is_not_a_phone():
    assert extract_contacts('<footer>Copyright 2010-2026</footer>')['phones'] == []


def test_homepage_meta_redirect_is_followed_without_exploring_links():
    seen = []
    def respond(request):
        seen.append(str(request.url))
        html = '<meta http-equiv="Refresh" content="0; URL=default/">' if request.url.path == '/' else CONTACTS
        return httpx.Response(200, text=html, headers={'content-type': 'text/html'})
    item = search.search_suppliers('딸기', transport=httpx.MockTransport(respond))['suppliers'][0]
    assert seen == ['https://supplier.test/', 'https://supplier.test/default/']
    assert item['contact_source_url'] == 'https://supplier.test/default/' and item['emails'] == ['sales@supplier.test']


@pytest.mark.parametrize('mode', ['redirect', 'notice'])
def test_google_goto_links_are_resolved(mode, monkeypatch):
    monkeypatch.setattr(search, '_search_page', lambda url: ('<a href="/goto?url=opaque"><h3>업체</h3></a>', url))
    seen = []
    def respond(request):
        seen.append(str(request.url))
        if request.url.host == 'www.google.com':
            if mode == 'redirect':
                return httpx.Response(302, headers={'location': 'https://supplier.test/product'})
            return httpx.Response(200, text='<a href="https://supplier.test/product">Continue</a><a href="https://policies.google.com/">Policy</a>')
        return httpx.Response(200, text=CONTACTS, headers={'content-type': 'text/html'})
    item = search.search_suppliers('딸기', transport=httpx.MockTransport(respond))['suppliers'][0]
    assert item['search_result_url'] == 'https://supplier.test/product'
    assert item['emails'] == ['sales@supplier.test']
    assert seen == ['https://www.google.com/goto?url=opaque', 'https://supplier.test/']


def test_unresolved_links_do_not_invent_homepages(monkeypatch):
    monkeypatch.setattr(search, '_search_page', lambda url: ('<a href="/goto?url=opaque"><h3>업체</h3></a>', url))
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text='<p>No target</p>'))
    item = search.search_suppliers('딸기', transport=transport)['suppliers'][0]
    assert item['homepage_url'] is None and item['emails'] == []
    assert item['contact_error'] == 'URL_UNRESOLVED'


def test_no_contacts_is_distinct_from_fetch_failure():
    for status, expected in [(200, 'NOT_FOUND'), (403, 'FETCH_FAILED')]:
        item = search.search_suppliers('딸기', transport=httpx.MockTransport(lambda _: httpx.Response(status, text='<p>상품</p>', headers={'content-type': 'text/html'})))['suppliers'][0]
        assert item['contact_status'] == expected and item['emails'] == item['phones'] == []


def test_homepage_redirects_and_site_deduplication(monkeypatch):
    source = HTML + '<a href="https://supplier.test/other"><h3>같은 사이트</h3></a>'
    monkeypatch.setattr(search, '_search_page', lambda url: (source, url))
    seen = []
    def respond(request):
        seen.append(str(request.url))
        if request.url.path == '/':
            return httpx.Response(302, headers={'location': '/ko/'})
        return httpx.Response(200, text=CONTACTS, headers={'content-type': 'text/html'})
    result = search.search_suppliers('딸기', transport=httpx.MockTransport(respond))
    assert seen == ['https://supplier.test/', 'https://supplier.test/ko/']
    assert len(result['suppliers']) == 1
    assert result['suppliers'][0]['contact_source_url'] == 'https://supplier.test/ko/'


@pytest.mark.parametrize('url', ['http://127.0.0.1/', 'http://192.168.1.1/', 'file:///etc/passwd', 'https://user:pass@supplier.test/'])
def test_private_or_invalid_homepages_never_requested(url, monkeypatch):
    if url.startswith('http:'):
        host = httpx.URL(url).host
        monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', (host, 80))])
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('Unsafe request'))) as client:
        assert collect_homepage_contacts(client, url)['contact_error'] == 'UNSAFE_URL'


def test_redirect_to_private_network_is_blocked(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda host, *args, **kwargs: [(2, 1, 6, '', ('127.0.0.1' if host == 'localhost' else '93.184.216.34', 443))])
    seen = []
    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'location': 'http://localhost/'})
    result = search.search_suppliers('딸기', transport=httpx.MockTransport(respond))
    assert seen == ['https://supplier.test/']
    assert result['suppliers'][0]['contact_error'] == 'UNSAFE_URL'


@pytest.mark.parametrize('body,current,expected', [
    ('<div class="g-recaptcha"></div>', 'https://www.google.com/sorry/', 'SEARCH_BLOCKED'),
    ('<html>unknown</html>', search.SEARCH_URL, 'SEARCH_FORMAT_CHANGED'),
])
def test_search_errors_are_not_empty(body, current, expected, monkeypatch):
    monkeypatch.setattr(search, '_search_page', lambda url: (body, current))
    assert search.search_suppliers('딸기')['code'] == expected


def test_retry_marker_in_normal_search_page_is_not_a_block(monkeypatch):
    monkeypatch.setattr(search, '_search_page', lambda url: (HTML + '<a href="/httpservice/retry/enablejs">retry</a>', url))
    result = search.search_suppliers('딸기', transport=httpx.MockTransport(lambda _: httpx.Response(200, text=CONTACTS, headers={'content-type': 'text/html'})))
    assert result['suppliers'][0]['emails'] == ['sales@supplier.test']


def test_empty_and_browser_failure(monkeypatch):
    monkeypatch.setattr(search, '_search_page', lambda url: ('일치하는 검색결과가 없습니다', url))
    assert search.search_suppliers('딸기')['status'] == 'EMPTY'
    monkeypatch.setattr(search, '_search_page', Mock(side_effect=RuntimeError('browser failed')))
    result = search.search_suppliers('딸기')
    assert result['code'] == 'SEARCH_BROWSER_ERROR' and result['error_type'] == 'RuntimeError'


def test_http_search_block_is_not_reported_as_empty(monkeypatch):
    monkeypatch.setattr(search, '_search_page', Mock(side_effect=search.SearchBlocked))
    assert search.search_suppliers('딸기')['code'] == 'SEARCH_BLOCKED'


def test_response_limits_and_timeouts():
    for response, expected in [(httpx.Response(200, content=b'x' * 2_000_001), 'RESPONSE_TOO_LARGE'),
                               (httpx.Response(200, headers={'content-type': 'application/pdf'}), 'NOT_HTML')]:
        result = search.search_suppliers('딸기', transport=httpx.MockTransport(lambda _: response))
        assert result['suppliers'][0]['contact_error'] == expected
    def timeout(request):
        raise httpx.ReadTimeout('timeout')
    result = search.search_suppliers('딸기', transport=httpx.MockTransport(timeout))
    assert result['suppliers'][0]['contact_error'] == 'ReadTimeout'


def test_tool_accepts_query_without_ids_or_db(monkeypatch):
    callback = Mock(return_value={'status': 'EMPTY', 'suppliers': []})
    monkeypatch.setattr(module, 'search_suppliers', callback)
    assert module.crawl_supplier_candidates.invoke({'query': ' 동결건조 딸기 '})['status'] == 'EMPTY'
    callback.assert_called_once_with('동결건조 딸기', 10)
    for args in [{'query': ' '}, {'query': '딸기', 'supplier_id': 'fake'}, {'query': '딸기', 'limit': 21}]:
        with pytest.raises(ValidationError):
            module.crawl_supplier_candidates.invoke(args)

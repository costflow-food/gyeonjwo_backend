import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Settings
from .errors import AppError

TABLES = frozenset(
    {
        "suppliers",
        "items",
        "contracts",
        "purchase_plans",
        "price_references",
        "cost_terms",
        "work_items",
        "request_fields",
        "review_results",
    }
)
RPCS = frozenset({"issue_work_id", "save_request_fields", "record_decision"})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def eq(value: str) -> str:
    return 'eq."' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SupabaseGateway:
    """Stateless REST transport: user JWTs never replace the server's DB credentials."""

    def __init__(self, settings: Settings, opener=None):
        self.settings = settings
        self.opener = opener or build_opener(NoRedirect())

    def request(
        self,
        method,
        path,
        *,
        params=None,
        body=None,
        data=None,
        content_type="application/json",
        token=None,
        binary=False,
        prefer=None,
    ):
        if not self.settings.ready:
            raise AppError(503, "not_configured", "서버의 Supabase 환경변수를 설정해 주세요.")
        headers = {
            "apikey": self.settings.service_key,
            "Content-Type": content_type,
            "User-Agent": "CostFlowBackend/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif not self.settings.service_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.settings.service_key}"
        if prefer:
            headers["Prefer"] = prefer
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        query = "?" + urlencode(params) if params else ""
        req = Request(
            self.settings.supabase_url + path + query, data=data, headers=headers, method=method
        )
        limit = self.settings.max_upload_bytes if binary else 8 * 1024 * 1024
        try:
            with self.opener.open(req, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(limit + 1)
        except HTTPError as exc:
            code = ""
            try:
                code = str(json.loads(exc.read(4096)).get("code", ""))
            except (ValueError, AttributeError):
                pass
            if path.startswith("/auth/") and exc.code in {400, 401, 403}:
                raise AppError(
                    401, "invalid_credentials", "로그인 정보 또는 토큰을 확인해 주세요."
                ) from None
            if code in {"23505", "23503", "23514", "P0001", "22003"}:
                raise AppError(
                    409, "database_conflict", "현재 업무 상태·입력값·DB 제약조건을 확인해 주세요."
                ) from None
            if code.startswith("22"):
                raise AppError(
                    422, "database_input", "DB가 허용하는 형식의 입력값이 필요합니다."
                ) from None
            if exc.code == 404:
                raise AppError(404, "not_found", "요청한 리소스를 찾을 수 없습니다.") from None
            raise AppError(
                503, "upstream_error", "Supabase 연결 또는 서버 권한을 확인해 주세요."
            ) from None
        except (URLError, TimeoutError, OSError):
            raise AppError(503, "upstream_unavailable", "Supabase에 연결하지 못했습니다.") from None
        if len(raw) > limit:
            raise AppError(413, "upstream_too_large", "응답 크기가 허용 범위를 초과했습니다.")
        if binary:
            return raw
        try:
            return json.loads(raw, parse_float=str) if raw else None
        except ValueError:
            raise AppError(
                502, "invalid_upstream", "Supabase 응답 형식을 확인할 수 없습니다."
            ) from None

    def rows(self, table: str, *, filters=None, limit=100, offset=0, order=None):
        if table not in TABLES:
            raise ValueError("Table is not allowed")
        params = {"select": "*", "limit": limit, "offset": offset} | (filters or {})
        if order:
            params["order"] = order
        result = self.request("GET", f"/rest/v1/{table}", params=params)
        if not isinstance(result, list):
            raise AppError(502, "invalid_upstream", "테이블 응답 형식이 올바르지 않습니다.")
        return result

    def rpc(self, name, body):
        if name not in RPCS:
            raise ValueError("RPC is not allowed")
        return self.request("POST", f"/rest/v1/rpc/{name}", body=body)

    def insert_work(self, body):
        return self.request(
            "POST", "/rest/v1/work_items", body=body, prefer="return=representation"
        )[0]

    def reject(self, work_id, reason):
        return self.request(
            "PATCH",
            "/rest/v1/work_items",
            params={"work_id": eq(work_id), "status": "eq.ON_HOLD"},
            body={"classification_reason": reason, "status": "REJECTED"},
            prefer="return=representation",
        )

    def user(self, token):
        return self.request("GET", "/auth/v1/user", token=token)

    def token(self, grant_type, body):
        return self.request("POST", "/auth/v1/token", params={"grant_type": grant_type}, body=body)

    def logout(self, token):
        self.request("POST", "/auth/v1/logout", params={"scope": "local"}, token=token)

    @property
    def bucket_path(self):
        return quote(self.settings.bucket, safe="")

    def upload(self, path, data, content_type):
        return self.request(
            "POST",
            f"/storage/v1/object/{self.bucket_path}/{quote(path, safe='/')}",
            data=data,
            content_type=content_type,
        )

    def files(self, work_id, limit=100, offset=0):
        return self.request(
            "POST",
            f"/storage/v1/object/list/{self.bucket_path}",
            body={
                "prefix": work_id + "/",
                "limit": limit,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
        )

    def download(self, path):
        return self.request(
            "GET",
            f"/storage/v1/object/authenticated/{self.bucket_path}/{quote(path, safe='/')}",
            binary=True,
        )

    def signed_link(self, path, expires_in):
        result = self.request(
            "POST",
            f"/storage/v1/object/sign/{self.bucket_path}/{quote(path, safe='/')}",
            body={"expiresIn": expires_in},
        )
        signed = result.get("signedURL") or result.get("signedUrl")
        if not isinstance(signed, str):
            raise AppError(502, "invalid_upstream", "서명 URL 응답이 올바르지 않습니다.")
        if signed.startswith("/object/"):
            return self.settings.supabase_url + "/storage/v1" + signed
        if signed.startswith("/storage/v1/"):
            return self.settings.supabase_url + signed
        p = urlsplit(signed)
        if p.scheme == "https" and p.netloc == urlsplit(self.settings.supabase_url).netloc:
            return signed
        raise AppError(502, "invalid_upstream", "예상하지 못한 서명 URL입니다.")

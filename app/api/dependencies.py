"""라우터들이 같이 쓰는 인증 헤더 읽기."""
from fastapi.security import HTTPBearer

bearer = HTTPBearer(auto_error=False)

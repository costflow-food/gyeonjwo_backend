"""시험 수집 설정. 서버 Settings와 같은 .env를 읽되 수집 전용 항목만 다룬다.

키는 각 발급처에서 우영님·팀원이 직접 받아 .env에만 넣는다. 값은 로그·리포트에 남기지 않는다.
"""
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import ROOT


class CrawlSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    # 식품안전나라 Open API 인증키 (품목제조보고·식품제조가공업 인허가)
    food_safety_api_key: SecretStr | None = None
    # 공공데이터포털 인증키 (한국식품안전관리인증원 HACCP 업체 정보)
    data_go_kr_service_key: SecretStr | None = None
    # 활용가이드 요청 예제 기준 오퍼레이션 경로 (상세기능 표의 getFoodCompanyListService와 다르면 .env로 바꾼다)
    haccp_api_url: str = 'https://apis.data.go.kr/B553748/FoodCompanyListService/getFoodCompanyList'
    # 네이버 지역 검색 API — NAVER API HUB 키(업체 홈페이지 주소 찾기). 없으면 시드 파일만 사용한다.
    naver_local_url: str = 'https://naverapihub.apigw.ntruss.com/search/v1/local'
    naver_webkr_url: str = 'https://naverapihub.apigw.ntruss.com/search/v1/webkr'
    naver_client_id: str = ''
    naver_client_secret: SecretStr | None = None
    crawl_user_agent: str = 'GyeonjwoSupplierTrial/0.1'
    crawl_delay_seconds: float = Field(default=1.0, ge=0.5, le=10)
    crawl_timeout_seconds: float = Field(default=10, ge=3, le=30)
    crawl_max_pages_per_site: int = Field(default=4, ge=1, le=10)

    @field_validator('food_safety_api_key', 'data_go_kr_service_key', 'naver_client_secret', mode='before')
    @classmethod
    def empty_secret(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    def secrets(self) -> list[str]:
        values = [self.food_safety_api_key, self.data_go_kr_service_key, self.naver_client_secret]
        return [value.get_secret_value() for value in values if value and value.get_secret_value()]

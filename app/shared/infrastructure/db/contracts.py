"""DB 함수가 돌려주는 거절 중 여러 기능이 같이 쓰는 예외."""


class OwnershipLost(RuntimeError):
    """DB rejected a stale token, expired lease or invalid state."""

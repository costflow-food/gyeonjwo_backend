#!/usr/bin/env bash
# 로컬 Docker 이미지를 Lightsail 컨테이너 서비스에 올리고 새 버전으로 배포한다.
# 배포 뒤 /health·/ready로 새 빌드가 실제로 일하는지 확인하고, 아니면 직전 배포로 되돌린다(KAN-202).
#
#   .github/scripts/deploy_lightsail.sh <로컬 이미지> <빌드 리비전>
#
# 환경변수
#   AWS_REGION      기본 ap-northeast-2
#   SERVICE         기본 gyeonjwo-api
#   ENV_FILE        (선택) 이 .env 파일로 컨테이너 환경변수를 새로 정한다.
#                   없으면 지금 실행 중인 배포의 환경변수를 그대로 쓴다(CI 기본).
#   VERIFY_TIMEOUT  배포 확인 최대 시간(초, 기본 300). 트래픽 전환 지연을 흡수한다.
#   VERIFY_INTERVAL 확인 간격(초, 기본 10)
#   VERIFY_PASSES   연속 통과 횟수(기본 3)
#   POLL_SECONDS    Lightsail 상태 확인 간격(초, 기본 10)
# 필요: aws CLI v2, lightsailctl, python3, curl
#
# 성공 판정(모두 VERIFY_PASSES회 연속):
#   - /health build_revision = 새 리비전
#   - /ready HTTP 200, status=ready, build_revision = 새 리비전
#   - /ready degraded_features ⊆ 배포 전 기준(배포 전 /ready가 200이면 그 degraded 집합,
#     배포 전 /ready가 200이 아니면 제한 없음(DB 장애 복구 배포 허용), 첫 배포면 빈 집합)
#     같은 DB를 쓰므로 DB 원인으로 이미 꺼져 있던 기능은 실패로 보지 않는다. 새 코드가 늘린 저하만 실패다.
# 실패하면 저장해 둔 직전 배포(이미지·환경변수·엔드포인트)를 다시 배포하고, 롤백 결과와 상관없이 CI는 실패로 끝낸다.
set -euo pipefail

image="${1:?usage: deploy_lightsail.sh <local-image> <revision>}"
revision="${2:?usage: deploy_lightsail.sh <local-image> <revision>}"
service="${SERVICE:-gyeonjwo-api}"
export AWS_REGION="${AWS_REGION:-ap-northeast-2}"
verify_timeout="${VERIFY_TIMEOUT:-300}"
verify_interval="${VERIFY_INTERVAL:-10}"
verify_passes="${VERIFY_PASSES:-3}"
poll_seconds="${POLL_SECONDS:-10}"
attempts=$(( verify_timeout / verify_interval ))   # 확인 시도 횟수(간격×횟수 ≈ VERIFY_TIMEOUT)
[ "$attempts" -ge "$verify_passes" ] || attempts="$verify_passes"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

ls_svc() { aws lightsail get-container-services --service-name "$service" --query "containerServices[0].$1" --output text; }

summary() {
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '%s\n' "$@" >> "$GITHUB_STEP_SUMMARY"
  fi
}

# GET <path> → $work/body 에 본문, 표준출력에 HTTP 코드(연결 실패는 000). 실패해도 스크립트를 멈추지 않는다.
fetch() {
  local code
  rm -f "$work/body"   # 실패한 요청이 이전 응답 본문을 남기지 않게 한다
  code="$(curl -sS -m 10 -o "$work/body" -w '%{http_code}' "${url%/}$1" 2>/dev/null)" || code="000"
  [ -s "$work/body" ] || echo '{}' > "$work/body"
  printf '%s' "${code:-000}"
}

# 본문 JSON의 필드 하나(문자열·리스트). 리스트는 쉼표로 이어 출력한다. JSON이 아니면 빈 값.
field() {
  python3 - "$work/body" "$1" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1])).get(sys.argv[2])
except Exception:
    value = None
if isinstance(value, list):
    print(",".join(sorted(str(v) for v in value)))
elif value is not None:
    print(value)
PY
}

# a(쉼표 목록)가 b(쉼표 목록)의 부분집합인지. b가 "*"이면 항상 참.
subset() {
  python3 - "$1" "$2" <<'PY'
import sys
a = {v for v in sys.argv[1].split(",") if v}
b = sys.argv[2]
sys.exit(0 if b == "*" or a <= {v for v in b.split(",") if v} else 1)
PY
}

# 배포 version의 상태가 ACTIVE가 될 때까지 기다린다. ACTIVE=0, FAILED=1, 시간 초과=2
wait_deployment() {
  local version="$1" state=""
  for _ in $(seq 1 90); do
    state="$(aws lightsail get-container-service-deployments --service-name "$service" \
      --query "deployments[?version==\`$version\`].state | [0]" --output text)"
    case "$state" in
      ACTIVE) return 0 ;;
      FAILED) return 1 ;;
    esac
    sleep "$poll_seconds"
  done
  return 2
}

# 서비스가 처음 만들어지는 중이면 준비될 때까지 기다린다.
for _ in $(seq 1 60); do
  state="$(ls_svc state)"
  case "$state" in READY|RUNNING) break ;; esac
  echo "service state: $state (waiting)"
  sleep "$poll_seconds"
done
url="$(ls_svc url)"

# 0) 롤백 기준: 지금 실행 중인 배포(이미지·환경변수·엔드포인트)와 그 빌드의 상태. 값은 로그에 찍지 않는다.
aws lightsail get-container-services --service-name "$service" \
  --query 'containerServices[0].currentDeployment' --output json > "$work/previous.json"
python3 - "$work/previous.json" "$work" <<'PY'
import json, os, sys
try:
    current = json.load(open(sys.argv[1])) or {}
except Exception:
    current = {}
containers, endpoint = current.get("containers"), current.get("publicEndpoint")
if containers and endpoint:
    json.dump(containers, open(os.path.join(sys.argv[2], "rollback_containers.json"), "w"))
    json.dump(endpoint, open(os.path.join(sys.argv[2], "rollback_endpoint.json"), "w"))
    print(current.get("version", ""), file=open(os.path.join(sys.argv[2], "previous_version"), "w"))
PY
previous_version="$(cat "$work/previous_version" 2>/dev/null || true)"
previous_revision=""
baseline="*"
if [ -n "$previous_version" ]; then
  if [ "$(fetch /health)" = 200 ]; then previous_revision="$(field build_revision)"; fi
  if [ "$(fetch /ready)" = 200 ] && [ "$(field status)" = ready ]; then baseline="$(field degraded_features)"; fi
  echo "previous: v$previous_version (${previous_revision:-unknown}), degraded before deploy: [${baseline/\*/not ready}]"
else
  baseline=""   # 첫 배포: 비교할 기준이 없으므로 저하가 하나라도 있으면 실패
  echo "previous: none (first deployment, rollback unavailable)"
fi

# 1) 이미지 업로드
out="$(aws lightsail push-container-image --service-name "$service" --label api --image "$image" 2>&1)" || { echo "$out"; exit 1; }
ref="$(printf '%s\n' "$out" | grep -oE "\":${service}\.api\.[0-9]+\"" | tail -1 | tr -d '"')"
[ -n "$ref" ] || { echo "$out"; echo "::error::업로드한 이미지 이름을 찾지 못했습니다"; exit 1; }
echo "pushed $ref"

# 2) 컨테이너 환경변수 (값은 로그에 찍지 않는다)
if [ -n "${ENV_FILE:-}" ]; then
  python3 - "$ENV_FILE" > "$work/env.json" <<'PY'
import json, sys
env = {}
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if value:
        env[key] = value
print(json.dumps(env))
PY
else
  aws lightsail get-container-services --service-name "$service" \
    --query 'containerServices[0].currentDeployment.containers.api.environment' --output json > "$work/env.json"
  if [ "$(tr -d '[:space:]' < "$work/env.json")" = "null" ]; then
    echo "::error::실행 중인 배포가 없어 환경변수를 가져올 수 없습니다. ENV_FILE로 처음 배포하세요."
    exit 1
  fi
fi

python3 - "$work/env.json" "$ref" > "$work/containers.json" <<'PY'
import json, sys
env = json.load(open(sys.argv[1]))
print(json.dumps({"api": {"image": sys.argv[2], "environment": env, "ports": {"8000": "HTTP"}}}))
PY
# Lightsail 헬스체크는 생존 확인(/health)으로 둔다. /ready로 바꾸면 DB 문제 때 컨테이너가 재시작을 반복한다.
cat > "$work/endpoint.json" <<'JSON'
{"containerName": "api", "containerPort": 8000,
 "healthCheck": {"path": "/health", "intervalSeconds": 10, "timeoutSeconds": 5,
                 "healthyThreshold": 2, "unhealthyThreshold": 5, "successCodes": "200"}}
JSON

# 실패 처리: 직전 배포로 되돌리고 CI를 실패로 끝낸다.
rollback_and_fail() {
  local reason="$1" rb_version rb_status="롤백 안 함"
  echo "::error::$reason"
  if [ -f "$work/rollback_containers.json" ]; then
    echo "rolling back to v$previous_version (${previous_revision:-unknown})"
    if rb_version="$(aws lightsail create-container-service-deployment --service-name "$service" \
         --containers "file://$work/rollback_containers.json" --public-endpoint "file://$work/rollback_endpoint.json" \
         --query 'containerService.nextDeployment.version' --output text)"; then
      local rc=0
      wait_deployment "$rb_version" || rc=$?
      if [ "$rc" = 0 ]; then
        rb_status="롤백 배포 v$rb_version ACTIVE"
        if [ -n "$previous_revision" ]; then
          local seen=""
          for _ in $(seq 1 "$attempts"); do
            if [ "$(fetch /health)" = 200 ]; then seen="$(field build_revision)"; fi
            [ "$seen" = "$previous_revision" ] && break
            sleep "$verify_interval"
          done
          if [ "$seen" = "$previous_revision" ]; then
            rb_status="롤백 완료: v$rb_version ($previous_revision) 응답 확인"
          else
            rb_status="롤백 배포는 ACTIVE지만 /health가 직전 리비전($previous_revision)을 보고하지 않음(마지막: ${seen:-없음})"
            echo "::error::$rb_status"
          fi
        fi
      else
        rb_status="롤백 배포 v$rb_version 실패(state rc=$rc). Lightsail 콘솔에서 확인 필요"
        echo "::error::$rb_status"
      fi
    else
      rb_status="롤백 배포 요청 실패. Lightsail 콘솔에서 확인 필요"
      echo "::error::$rb_status"
    fi
  else
    echo "::warning::직전 배포가 없어 롤백할 수 없습니다"
  fi
  echo "rollback: $rb_status"
  summary "### 서버 배포 실패: $revision" "- 원인: $reason" "- 롤백: $rb_status"
  exit 1
}

# 3) 배포
version="$(aws lightsail create-container-service-deployment --service-name "$service" \
  --containers "file://$work/containers.json" --public-endpoint "file://$work/endpoint.json" \
  --query 'containerService.nextDeployment.version' --output text)"
echo "deployment v$version started"

rc=0
wait_deployment "$version" || rc=$?
if [ "$rc" = 1 ]; then
  # Lightsail은 실패한 배포를 활성화하지 않고 직전 배포를 계속 서비스한다.
  echo "::error::배포 v$version 실패. 직전 배포가 계속 서비스됩니다. Lightsail 콘솔의 로그를 확인하세요."
  summary "### 서버 배포 실패: $revision" "- 원인: Lightsail 배포 v$version FAILED(직전 배포 유지)"
  exit 1
elif [ "$rc" != 0 ]; then
  rollback_and_fail "배포 v$version 대기 시간 초과"
fi

# 4) 새 빌드가 응답하고 기능이 살아 있는지 확인(트래픽 전환 지연을 재시도로 흡수)
passes=0
reason="확인 전"
degraded=""
for _ in $(seq 1 "$attempts"); do
  ok=1
  code="$(fetch /health)"
  health_rev="$(field build_revision)"
  if [ "$code" != 200 ] || [ "$health_rev" != "$revision" ]; then
    ok=0; reason="/health HTTP $code, build_revision=${health_rev:-없음}(기대 $revision)"
  else
    code="$(fetch /ready)"
    ready_rev="$(field build_revision)"; ready_status="$(field status)"; degraded="$(field degraded_features)"
    if [ "$ready_rev" != "$revision" ]; then
      ok=0; reason="/ready build_revision=${ready_rev:-없음}(기대 $revision)"
    elif [ "$code" != 200 ] || [ "$ready_status" != ready ]; then
      ok=0; reason="/ready HTTP $code, status=${ready_status:-없음}"
    elif ! subset "$degraded" "$baseline"; then
      ok=0; reason="/ready degraded_features=[$degraded], 배포 전=[$baseline]"
    fi
  fi
  if [ "$ok" = 1 ]; then
    passes=$((passes + 1))
    echo "verify pass $passes/$verify_passes (degraded=[$degraded])"
    [ "$passes" -ge "$verify_passes" ] && break
  else
    [ "$passes" -gt 0 ] && echo "verify reset: $reason"
    passes=0
  fi
  sleep "$verify_interval"
done

if [ "$passes" -lt "$verify_passes" ]; then
  rollback_and_fail "배포 v$version 확인 실패(${verify_timeout}초): $reason"
fi

echo "deployed v$version ($revision) → $url"
summary "### 서버 배포 완료: v$version ($revision) → $url" \
        "- /ready: ready, degraded_features=[${degraded}] (배포 전 [${baseline/\*/확인 불가}])"

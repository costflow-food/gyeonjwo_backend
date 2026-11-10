#!/usr/bin/env bash
# 로컬 Docker 이미지를 Lightsail 컨테이너 서비스에 올리고 새 버전으로 배포한다.
#
#   .github/scripts/deploy_lightsail.sh <로컬 이미지> <빌드 리비전>
#
# 환경변수
#   AWS_REGION  기본 ap-northeast-2
#   SERVICE     기본 gyeonjwo-api
#   ENV_FILE    (선택) 이 .env 파일로 컨테이너 환경변수를 새로 정한다.
#               없으면 지금 실행 중인 배포의 환경변수를 그대로 쓴다(CI 기본).
# 필요: aws CLI v2, lightsailctl, python3, curl
set -euo pipefail

image="${1:?usage: deploy_lightsail.sh <local-image> <revision>}"
revision="${2:?usage: deploy_lightsail.sh <local-image> <revision>}"
service="${SERVICE:-gyeonjwo-api}"
export AWS_REGION="${AWS_REGION:-ap-northeast-2}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

ls_svc() { aws lightsail get-container-services --service-name "$service" --query "containerServices[0].$1" --output text; }

# 서비스가 처음 만들어지는 중이면 준비될 때까지 기다린다.
for _ in $(seq 1 60); do
  state="$(ls_svc state)"
  case "$state" in READY|RUNNING) break ;; esac
  echo "service state: $state (waiting)"
  sleep 10
done

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
cat > "$work/endpoint.json" <<'JSON'
{"containerName": "api", "containerPort": 8000,
 "healthCheck": {"path": "/health", "intervalSeconds": 10, "timeoutSeconds": 5,
                 "healthyThreshold": 2, "unhealthyThreshold": 5, "successCodes": "200"}}
JSON

# 3) 배포
version="$(aws lightsail create-container-service-deployment --service-name "$service" \
  --containers "file://$work/containers.json" --public-endpoint "file://$work/endpoint.json" \
  --query 'containerService.nextDeployment.version' --output text)"
echo "deployment v$version started"

state=""
for _ in $(seq 1 90); do
  state="$(aws lightsail get-container-service-deployments --service-name "$service" \
    --query "deployments[?version==\`$version\`].state | [0]" --output text)"
  case "$state" in
    ACTIVE) break ;;
    FAILED) echo "::error::배포 v$version 실패. Lightsail 콘솔의 로그를 확인하세요."; exit 1 ;;
  esac
  sleep 10
done
[ "$state" = ACTIVE ] || { echo "::error::배포 v$version 대기 시간 초과 (state=$state)"; exit 1; }

# 4) 새 빌드가 응답하는지 확인
url="$(ls_svc url)"
health="$(curl -fsS --retry 6 --retry-delay 5 --retry-all-errors "${url%/}/health")"
echo "$health"
printf '%s' "$health" | grep -q "\"build_revision\":\"$revision\"" \
  || { echo "::error::/health가 새 리비전($revision)을 보고하지 않습니다"; exit 1; }

echo "deployed v$version ($revision) → $url"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "### 서버 배포 완료: v$version ($revision) → $url" >> "$GITHUB_STEP_SUMMARY"
fi

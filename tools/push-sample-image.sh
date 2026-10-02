#!/usr/bin/env bash
# 샘플 이미지(examples/sample-app)를 linux/amd64 로 빌드해 팀 계정 ECR 에 푸시한다.
# EC2 한 바퀴(배포 → 접속 → 삭제)를 돌리기 전 준비 단계. Git Bash(Windows) / macOS / Linux 공통.
#
#   tools/push-sample-image.sh [리전] [저장소] [태그]
#   예) tools/push-sample-image.sh ap-northeast-2 pawploy-sample latest
#
# 끝나면 image_uri 를 출력한다. 그 값을 examples/job-ec2.json 의 image_uri 에 넣으면 된다.
# 필요: aws CLI 로그인 상태(aws sts get-caller-identity), Docker(buildx 포함).
set -euo pipefail

REGION="${1:-ap-northeast-2}"
REPO="${2:-pawploy-sample}"
TAG="${3:-latest}"
APP_DIR="$(cd "$(dirname "$0")/../examples/sample-app" && pwd)"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${REPO}:${TAG}"

echo "[push] 계정 ${ACCOUNT}, 리전 ${REGION}, 이미지 ${IMAGE_URI}"

# 저장소가 없으면 만든다 (푸시 시 취약점 스캔, 태그 덮어쓰기 허용)
if ! aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1; then
  echo "[push] ECR 저장소 생성: ${REPO}"
  aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --tags Key=pawploy:managed,Value=true >/dev/null
fi

aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

# --platform linux/amd64 : EC2(x86_64)·Lambda(x86_64) 둘 다 이 아키텍처로 배포한다 (Apple Silicon 에서도 필수)
# --provenance=false --sbom=false : buildx 기본값은 증명(attestation) 매니페스트를 붙여 "이미지 인덱스"를 만드는데,
#                                    Lambda 는 단일 매니페스트만 받는다. 이 두 옵션이 없으면 Lambda 생성이 실패한다
docker buildx build \
  --platform linux/amd64 \
  --provenance=false --sbom=false \
  --tag "${IMAGE_URI}" \
  --push \
  "${APP_DIR}"

DIGEST="$(aws ecr describe-images --repository-name "${REPO}" --image-ids imageTag="${TAG}" \
  --region "${REGION}" --query 'imageDetails[0].imageDigest' --output text)"

echo
echo "[push] 완료"
echo "  image_uri (태그):    ${IMAGE_URI}"
echo "  image_uri (digest):  ${REGISTRY}/${REPO}@${DIGEST}"
echo "  → examples/job-ec2.json 의 image_uri 에 넣고: python -m tfworker deploy examples/job-ec2.json"

#!/usr/bin/env bash
# Pawploy Terraform Worker 운영 인프라 (팀 AWS 계정에 한 번만). 이미 있는 것은 건너뛴다.
#
#   bash infra/setup-aws.sh            # 무엇을 만들지 출력만 (조회만 하고 아무것도 만들지 않음)
#   bash infra/setup-aws.sh --apply    # 실제로 생성
#
# 만드는 것 (모두 사용량 과금, 시험 규모에서는 월 수백 원 이하)
#   S3 버킷       pawploy-tf-<계정>-<리전>   state(deployments/)·작업 폴더 보관(workdirs/). 비공개·암호화·버전 관리
#   DynamoDB 테이블 pawploy-deployments      배포 결과(Main Server 가 읽음) + 배포 잠금. 온디맨드
#   SQS 큐        pawploy-destroy            만료 시각 destroy 예약 메시지
#   IAM 역할      ppw-scheduler              EventBridge Scheduler 가 그 큐에만 메시지를 보낼 수 있는 역할
#
# 워커 실행 역할(최소 권한)과 GCP 키 보관(Secrets Manager)은 실행 방식(SQS 소비 / ECS 작업 등)이 정해진 뒤 만든다.
# (역할을 누가 맡을지 = 신뢰 정책이 실행 방식에 따라 달라지기 때문)
set -euo pipefail

REGION=${PAWPLOY_REGION:-ap-northeast-2}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=${PAWPLOY_BUCKET:-pawploy-tf-$ACCOUNT-$REGION}
TABLE=pawploy-deployments
QUEUE=pawploy-destroy
SCHED_ROLE=ppw-scheduler   # 플랫폼 역할은 ppw- 접두사: 워커가 다루는 pawploy-* 역할과 섞이지 않게
APPLY=false
[ "${1:-}" = "--apply" ] && APPLY=true

run() {   # 실행할 명령을 보여 주고, --apply 일 때만 실제로 실행
  echo "  + $*"
  if $APPLY; then "$@" > /dev/null; fi
}

echo "계정 $ACCOUNT / 리전 $REGION / $($APPLY && echo '실제 생성' || echo '출력만 (--apply 로 실행)')"

echo "[1/4] S3 버킷 $BUCKET"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "  이미 있음"
else
  run aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION"
  run aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  run aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  # 버전 관리: state 를 실수로 덮어써도 되돌릴 수 있게. 지난 버전은 30일 뒤 삭제
  run aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
  run aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"expire-old-versions","Status":"Enabled","Filter":{},"NoncurrentVersionExpiration":{"NoncurrentDays":30},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}'
  # 작업 폴더에는 사용자 환경변수 값이 평문으로 들어 있다 → HTTPS 가 아닌 접근 거부
  run aws s3api put-bucket-policy --bucket "$BUCKET" --policy \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"DenyInsecureTransport\",\"Effect\":\"Deny\",\"Principal\":\"*\",\"Action\":\"s3:*\",\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"],\"Condition\":{\"Bool\":{\"aws:SecureTransport\":\"false\"}}}]}"
  run aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=pawploy:platform,Value=true}]'
fi

echo "[2/4] DynamoDB 테이블 $TABLE"
if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" > /dev/null 2>&1; then
  echo "  이미 있음"
else
  run aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=deploy_id,AttributeType=S \
    --key-schema AttributeName=deploy_id,KeyType=HASH --billing-mode PAY_PER_REQUEST \
    --tags Key=pawploy:platform,Value=true
fi

echo "[3/4] SQS 큐 $QUEUE"
if QUEUE_URL=$(aws sqs get-queue-url --queue-name "$QUEUE" --region "$REGION" --query QueueUrl --output text 2>/dev/null); then
  echo "  이미 있음: $QUEUE_URL"
else
  # visibility timeout 30분: destroy 가 길어져도 다른 워커가 같은 메시지를 바로 다시 받지 않게
  run aws sqs create-queue --queue-name "$QUEUE" --region "$REGION" --attributes \
    '{"VisibilityTimeout":"1800","MessageRetentionPeriod":"345600","SqsManagedSseEnabled":"true"}' \
    --tags pawploy:platform=true
  QUEUE_URL="https://sqs.$REGION.amazonaws.com/$ACCOUNT/$QUEUE"
fi
QUEUE_ARN="arn:aws:sqs:$REGION:$ACCOUNT:$QUEUE"

echo "[4/4] IAM 역할 $SCHED_ROLE (Scheduler → 큐에 메시지 보내기만)"
if aws iam get-role --role-name "$SCHED_ROLE" > /dev/null 2>&1; then
  echo "  이미 있음"
else
  run aws iam create-role --role-name "$SCHED_ROLE" --tags Key=pawploy:platform,Value=true --assume-role-policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"scheduler.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT\"}}}]}"
  run aws iam put-role-policy --role-name "$SCHED_ROLE" --policy-name send-destroy --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sqs:SendMessage\",\"Resource\":\"$QUEUE_ARN\"}]}"
fi

cat <<EOF

워커 환경변수 (실행 환경에 넣을 값)
  PAWPLOY_REGION=$REGION
  PAWPLOY_STATE_BUCKET=$BUCKET
  PAWPLOY_ARTIFACT_BUCKET=$BUCKET
  PAWPLOY_STATUS_TABLE=$TABLE
  PAWPLOY_DESTROY_QUEUE_ARN=$QUEUE_ARN
  PAWPLOY_DESTROY_QUEUE_URL=$QUEUE_URL
  PAWPLOY_SCHEDULER_ROLE_ARN=arn:aws:iam::$ACCOUNT:role/$SCHED_ROLE
EOF

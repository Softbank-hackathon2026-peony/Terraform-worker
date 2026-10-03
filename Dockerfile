# Pawploy Terraform Worker 실행 이미지 (python + terraform + aws CLI)
#
#   docker build --platform linux/amd64 -t pawploy-tf-worker .
#   docker run --rm pawploy-tf-worker deploy /jobs/job.json      # 명령은 python -m tfworker 의 인자
#   docker run --rm pawploy-tf-worker sweep                       # 만료 정기 점검
#   docker run --rm pawploy-tf-worker drain-destroy-queue         # 만료 예약(SQS) 처리
#
# 컨테이너의 작업 폴더(/work)는 실행이 끝나면 사라진다. 클라우드에서 돌릴 때는 반드시
#   PAWPLOY_STATE_BUCKET, PAWPLOY_ARTIFACT_BUCKET, PAWPLOY_STATUS_TABLE 을 지정해 state·코드·결과를 밖에 둔다.
# AWS 권한은 실행 환경의 IAM 역할(ECS 작업 역할 등)로, GCP 키는 비밀 저장소에서 파일로 넣고
#   GOOGLE_APPLICATION_CREDENTIALS 로 경로를 알려 준다 (이미지에 키를 넣지 않는다).
FROM python:3.12-slim

ARG TERRAFORM_VERSION=1.16.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && cd /tmp \
 && curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
 && curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
 && grep " terraform_${TERRAFORM_VERSION}_linux_amd64.zip$" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" | sha256sum -c - \
 && unzip -q "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -d /usr/local/bin \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip \
 && unzip -q awscliv2.zip && ./aws/install \
 && rm -rf /tmp/* /var/lib/apt/lists/* \
 && terraform -version && aws --version

# DynamoDB 상태 기록용 (store.py). 호출마다 aws CLI 프로세스를 띄우면 약 1.8초라 클라이언트를 재사용한다
RUN pip install --no-cache-dir boto3==1.43.108

RUN useradd --create-home --uid 10001 worker && mkdir -p /work && chown worker /work
WORKDIR /app
COPY tfworker/ tfworker/
COPY modules/ modules/

USER worker
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PAWPLOY_WORK_DIR=/work \
    TF_PLUGIN_CACHE_DIR=/home/worker/.terraform.d/plugin-cache
RUN mkdir -p "$TF_PLUGIN_CACHE_DIR"

ENTRYPOINT ["python", "-m", "tfworker"]

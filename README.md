# Pawploy Terraform Worker

AgentCore 가 만든 Terraform 을 받아 **사용자가 승인한 클라우드(AWS·GCP, 하나 또는 둘 다)에 배포하고 접속 주소를 돌려주는** 워커입니다.
Pawploy 배포 파이프라인의 **21~27단계**를 맡습니다.

- 지원: AWS `ec2`·`lambda`, GCP `cloud_run`. AWS 와 GCP 를 함께 배포할 수 있습니다
- 워커는 Terraform 을 만들지 않습니다(AI 없음). 받은 코드를 **검증하고 실행**만 합니다
- 배포는 최대 60분 뒤 만료되고, 세 겹으로 지웁니다 → [1시간 자동 삭제](#1시간-자동-삭제)
- Python 표준 라이브러리만 사용합니다. 클라우드 호출은 `terraform`과 `aws` CLI로 합니다

---

## 파이프라인에서의 위치

```
05~17  AgentCore 분석 ↔ CodeBuild 사전 빌드(ECR + Artifact Registry 푸시) ↔ 사용자 승인(클라우드 선택)
18~20  Main → AgentCore: 승인된 클라우드별 Terraform 모듈 생성 → S3 저장
┌ 21   Main → Worker: IaC 검증 · 현재 상태 확인 · plan · apply 요청        ┐
│ 22   Worker → Main: 실패 단계 · 오류 로그 · 현재 상태  (클라우드별)        │  ← 이 저장소
│ 23~25 Main → AgentCore: 수정본 → S3 → 다시 21 (실패한 클라우드만)         │
│ 26   AppResources → Worker: 리소스 · 접속 URL 확인 (헬스체크)            │
└ 27   Worker → Main: 배포 상태 · 리소스 · 접속 URL  (클라우드별)            ┘
```

## 클라우드(target)별 처리 순서

```
preparing ─ generating ─────────────────── init ─ plan ──────────────── apply ─ health_check ─ running
 ECR 확인    모듈 가져오기(terraform_uri)          정책 검사(policy.py)          URL 응답 확인
 (AWS)       + 루트 main.tf(provider·태그)          → S3 보관
             + IaC 정적 검사(iac.py)
   │             │                         │       │                     │         │
   └─────────────┴──────── 실패 ───────────┴───────┘                     └── 실패 ──┘
            아무것도 만들지 않음 → failed                       앱 로그 수집 → destroy → failed
```

- **어느 단계에서 실패하든** 만든 리소스를 지운 뒤 `failed` 로 보고합니다. 그래서 수정본(23~25) 재요청은 **항상 빈 상태에서** 다시 배포됩니다.
- AWS·GCP 를 함께 배포하다 **한쪽이 실패해도 성공한 쪽은 그대로 둡니다** (전체 상태 `partial`). 재요청에는 실패한 클라우드만 넣으면 됩니다.

| 파일 | 하는 일 |
|---|---|
| `tfworker/job.py` | 21단계 입력 검증: 클라우드·아키텍처·이미지 주소(ECR / Artifact Registry digest)·크기·환경변수·TTL |
| `tfworker/image.py` | AWS: ECR 에 이미지가 있는지 확인하고 태그를 `@sha256:` digest 로 고정 |
| `tfworker/render.py` | `work/<deploy_id>/<cloud>/` 생성: 모듈 가져오기 + 루트 `main.tf`(provider·필수 태그/label·backend) |
| `tfworker/iac.py` | **IaC 정적 검사** (plan 전): 금지 문법·허용 리소스·data 소스·파일 읽기·입출력 약속 |
| `tfworker/terraform.py` | `init → plan → show -json → apply → output → state list` |
| `tfworker/policy.py` | **plan 정책 검사** (apply 전): 리소스 종류·크기·IAM·포트·필수 태그/label·Cloud Run 설정 |
| `tfworker/artifacts.py` | 검사를 통과한 작업 폴더를 S3 에 보관 (다른 머신에서도 같은 코드로 destroy) |
| `tfworker/health.py` | 26단계: 접속 URL 이 응답할 때까지 대기 |
| `tfworker/diagnose.py` | 응답 없음일 때 지우기 전에 앱 로그 수집 (EC2 콘솔 / Lambda 로그) |
| `tfworker/store.py` | `result.json` 과 같은 내용을 DynamoDB 에도 기록 (Main Server 가 읽음) |
| `tfworker/expire.py` | 만료된 배포 찾기(`sweep`), 태그로 남은 AWS 리소스 찾기(`orphans`) |

---

## 사용법

```bash
python -m tfworker deploy <작업.json | s3://…>     # 21 → 27 (클라우드에서는 Main 이 S3 에 올린 작업 JSON 경로)
python -m tfworker status <deploy_id>              # 결과 (work/<deploy_id>/result.json)
python -m tfworker destroy <deploy_id> [aws|gcp]   # 삭제 (클라우드 하나만도 가능)
python -m tfworker sweep [--dry-run]               # 만료된 배포를 모두 삭제 (5~10분마다 정기 실행)
python -m tfworker orphans [region]                # 태그로 만료 지난 AWS 리소스 찾기 (삭제 안 함, 있으면 exit 1)
python -m tfworker drain-destroy-queue [queue_url]  # 만료 예약(SQS) 메시지를 모두 받아 destroy
python -m tfworker maintenance                     # 정기 실행 한 번에: 만료 큐 처리 + sweep (5~10분마다)
```

### 컨테이너 이미지 (클라우드에서 실행)

```bash
docker build --platform linux/amd64 -t pawploy-tf-worker .     # python + terraform 1.16 + aws CLI, root 아닌 사용자
docker run --rm -e PAWPLOY_STATE_BUCKET=... -e PAWPLOY_ARTIFACT_BUCKET=... -e PAWPLOY_STATUS_TABLE=... \
  pawploy-tf-worker deploy /jobs/job.json
```

컨테이너의 `/work` 는 실행이 끝나면 사라지므로, 클라우드에서는 **S3 버킷·DynamoDB 테이블을 반드시 지정**합니다
(없으면 state 가 사라져 destroy 를 못 하고 리소스가 남습니다). 필요한 인프라는 `bash infra/setup-aws.sh` 로 확인하고 `--apply` 로 만듭니다.
AWS 권한은 실행 환경의 IAM 역할로, GCP 키는 비밀 저장소에서 파일로 넣어 `GOOGLE_APPLICATION_CREDENTIALS` 로 지정합니다 (이미지에 넣지 않음).

> Windows에서 `python`이 동작하지 않으면 `py -m tfworker ...`로 실행하세요.

종료 코드: `0` 모든 클라우드 성공 · `1` 하나라도 실패(결과는 클라우드별로 기록) · `2` 입력 오류(아무것도 하지 않음)

### 준비

| 도구 | 확인 명령 |
|---|---|
| Python 3.10 이상 | `py --version` (macOS 기본 python3 는 3.9 라 Homebrew python3.11 등을 쓸 것) |
| Terraform 1.10 이상 | `terraform -version` (S3 state 잠금 `use_lockfile` 이 1.10 부터) |
| AWS CLI v2 + 로그인 | `aws sts get-caller-identity` |
| GCP 배포 시: 서비스 계정 키 | 아래 [GCP 준비](#gcp-준비) |

### 비용 없이 시험하기

```bash
python -m unittest -v      # 자동 테스트 27개 (가짜 terraform·aws, 10~30초)

# 실제 terraform 으로 문법만 (provider 다운로드만, 클라우드는 부르지 않음)
terraform -chdir=work/<deploy_id>/<cloud> init -backend=false && terraform -chdir=work/<deploy_id>/<cloud> validate
```

`PAWPLOY_OFFLINE=1` 은 AWS 를 부르는 단계(이미지 확인·S3·DynamoDB·로그)를 건너뜁니다.
가짜 terraform(`tools/fake-terraform.py`)은 `FAKE_TF_FAIL=apply`·`FAKE_TF_FAIL_CLOUD=gcp` 로 특정 클라우드의 특정 단계를 실패시킬 수 있습니다.

---

## 입력 형식 (21단계: Main Server → Worker)

```json
{
  "deploy_id": "dep-demo",
  "project_id": "prj_demo",
  "container_port": 8080,
  "size": "small",
  "health_path": "/",
  "env": { "APP_MODE": "test" },
  "targets": [
    {
      "cloud": "aws",
      "architecture": "ec2",
      "image_uri": "<계정>.dkr.ecr.ap-northeast-2.amazonaws.com/<저장소>@sha256:<digest>",
      "terraform_uri": "s3://<버킷>/<project_id>/<deploy_id>/aws/"
    },
    {
      "cloud": "gcp",
      "architecture": "cloud_run",
      "image_uri": "asia-northeast3-docker.pkg.dev/<GCP프로젝트>/<저장소>/<이름>@sha256:<digest>",
      "terraform_uri": "s3://<버킷>/<project_id>/<deploy_id>/gcp/"
    }
  ]
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `deploy_id` | ✅ | 소문자·숫자·하이픈 4~40자. 리소스 이름 `pawploy-<deploy_id>` 에 쓰임. 재시도(23~25)는 **같은 값**으로, 새 배포는 새 값으로 |
| `project_id` | ✅ | 태그·state 경로에 쓰임 |
| `targets` | ✅ | 승인된 클라우드마다 하나 (최대 AWS 1 + GCP 1) |
| `targets[].cloud` | ✅ | `aws` / `gcp` |
| `targets[].architecture` | | **생략 가능.** AgentCore 모듈의 대표 리소스로 워커가 판단(`aws_instance`→`ec2`, `aws_lambda_function`→`lambda`, `google_cloud_run_v2_service`→`cloud_run`). 넘기면 모듈과 다를 때 `generating` 실패. 모듈이 없으면 AWS `ec2`·GCP `cloud_run` 기본 모듈 |
| `targets[].image_uri` | ✅ | AWS: ECR 주소(태그면 워커가 digest 로 고정). GCP: Artifact Registry 주소 + **`@sha256:` digest 필수** |
| `targets[].terraform_uri` | | **보통 생략.** 없으면 `PAWPLOY_AGENT_BUCKET` 의 `projects/<project_id>/deploy/<deploy_id>/attempt-<N>/<cloud>/` 중 최신 attempt. 그것도 없으면 기본 모듈(`modules/<architecture>`) |
| `targets[].region` | | AWS: 생략하면 ECR 주소의 리전(Lambda 는 같아야 함). GCP: 생략하면 Artifact Registry 리전 |
| `targets[].gcp_project` | | 생략하면 Artifact Registry 주소의 프로젝트 |
| `container_port`, `size`, `health_path`, `env` | | 기본 8080 / `small`(`micro`·`small`·`medium`) / `/` / `{}`. 모든 클라우드에 같이 적용 |
| `ttl_minutes` | | 5~60, 기본 60 |

- 이미 살아 있는 클라우드를 다시 보내면 거부합니다(종료 코드 2). 재시도는 `failed`·`destroyed` 인 클라우드만 넣습니다.
- 다른 클라우드가 살아 있는 상태의 재시도는 **처음 만료 시각을 그대로 씁니다** (재시도로 1시간 제한이 늘어나지 않음).
- `targets` 없이 `architecture`·`image_uri` 를 최상위에 두면 AWS target 하나로 봅니다 (이전 형식, `examples/job-ec2.json`).

### AgentCore 가 만들 Terraform 모듈 (18~19단계) — AgentCore 담당과 맞출 약속

`terraform_uri` 폴더에는 **모듈 하나**만 둡니다. provider·backend·필수 태그는 워커가 루트에서 정하므로 쓰지 않습니다.
가장 쉬운 방법은 `modules/ec2`·`modules/lambda`·`modules/cloud_run` 을 베이스로 고치는 것입니다(실제 배포로 검증된 코드).

- 파일: `.tf`·`.tftpl` 만, 하위 폴더 없이
- 입력 변수: `name`, `image_uri`, `container_port`, `size`, `env`, `health_path` (이름·의미 고정)
- 출력: `endpoint`, `health_url`, `resource_id`
- 리소스는 `var.name` 으로 시작하는 이름 (IAM `name_prefix` 는 `substr(var.name, 0, 37)`)

워커가 거부하는 것 (`iac.py`, plan 전):

| 금지 | 이유 |
|---|---|
| `provider`·`backend`·`cloud`·`module` 블록, `default_tags`·`default_labels` | 워커가 정한 계정·state·필수 태그를 바꿀 수 있음 |
| `provisioner`, `local-exec`/`remote-exec`, `data "external"` 등 허용 목록 밖 data 소스 | plan·apply 때 워커 컴퓨터에서 명령 실행, 팀 비밀값(SSM·Secrets Manager) 읽기 |
| `aws_ssm_parameter` 중 `/aws/service/...` 가 아닌 것, `access_token` | 팀 비밀값·워커 GCP 토큰을 앱 환경변수로 흘릴 수 있음 |
| `file()`·`templatefile()` 의 경로가 `"${path.module}/..."` 가 아닌 것 | 워커 컴퓨터의 자격 증명 파일을 읽어 앱으로 넘길 수 있음 |
| 허용 목록 밖 리소스 (`policy.ALLOWED_TYPES`), 허용 밖 IAM 정책·인라인 정책 | 우리 계정 권한 탈취·비용 |
| `iam_instance_profile`·`role`·`service_account` 에 문자열 직접 쓰기 (예외: `roles/run.invoker`) | 계정에 이미 있는 관리자 역할·GCP 기본 계정(편집자)을 앱에 붙일 수 있음 |
| EC2 `cpu_credits` 가 `standard` 가 아님, Cloud Run `deletion_protection` 이 `false` 가 아님 | 추가 과금 / 1시간 뒤 destroy 실패 |

plan 결과는 apply 전에 `policy.py` 가 한 번 더 검사합니다(앱 권한은 이 배포에서 새로 만든 역할·프로필·서비스 계정만, Cloud Run 은 서비스 계정 지정 필수, 만료 예약은 워커 루트에서만, 인스턴스 타입, Lambda 메모리·타임아웃, 인바운드 80번만, Cloud Run 메모리 2Gi·인스턴스 1개 이하, 공개 호출 권한은 `roles/run.invoker → allUsers` 만, 필수 태그/label).

## 결과 형식 (22·27단계: Worker → Main Server)

`work/<deploy_id>/result.json` (같은 내용이 DynamoDB `PAWPLOY_STATUS_TABLE` 에도):

```json
{
  "deploy_id": "dep-demo",
  "project_id": "prj_demo",
  "status": "partial",
  "expires_at": "2026-10-02T13:00:00Z",
  "targets": {
    "aws": {
      "architecture": "ec2", "status": "running",
      "endpoint": "http://3.38.1.2", "health_url": "http://3.38.1.2/",
      "resource_id": "i-0abc...", "image_uri": "...@sha256:...", "deploy_seconds": 249
    },
    "gcp": {
      "architecture": "cloud_run", "status": "failed",
      "failed_stage": "apply",
      "error": "terraform apply -auto-approve tfplan 실패 (exit 1)",
      "log_tail": "Error: ... (terraform 로그 마지막 4000자)",
      "app_log": null,
      "destroyed": true,
      "current_state": []
    }
  }
}
```

| 필드 | 뜻 |
|---|---|
| `status` (전체) | `deploying` · `running`(모두 성공) · `partial`(일부 성공) · `failed`(모두 실패, 남은 리소스 없음) · `destroying` · `destroyed` · `destroy_failed` · `rejected`(입력 오류로 시작하지 않음) |
| `error` (최상위) | `rejected` 일 때 입력 오류 이유 |
| `last_rejection` | 이미 있는 배포에 대한 요청이 거부됐을 때 `{error, at}`. 기존 상태는 바뀌지 않음 |
| `targets.<cloud>.status` | `preparing` → `generating` → `init` → `plan` → `apply` → `health_check` → `running` / `failed` / `destroying` → `destroyed` / `destroy_failed` |
| `failed_stage` | 22단계 "실패 단계". 위 상태 이름 중 하나 (`generating` 이면 IaC 검사 위반, `plan` 이면 정책 위반도 포함) |
| `error`, `log_tail` | 22단계 "오류 로그". AgentCore 에 수정을 맡길 때 그대로 넘기면 됨 |
| `app_log` | 응답 없음(`health_check`)일 때 지우기 전에 모은 앱 로그 (EC2·Lambda) |
| `current_state` | 22단계 "현재 상태". 정리 뒤 state 에 남은 리소스 주소. 정상이면 `[]` |
| `destroyed` | 실패 뒤 정리(destroy)를 했는지. apply 전 실패는 만든 게 없어 `false` |

---

## GCP 준비

GCP 배포에는 **서비스 계정 키**가 필요합니다. 서비스 계정 이메일(`...@<프로젝트>.iam.gserviceaccount.com`)이 아니라,
그 계정으로 로그인할 수 있는 **JSON 키 파일**입니다. 비밀번호처럼 다루고 **절대 커밋하지 마세요.**

1. 사용할 API 켜기: `run.googleapis.com`, `iam.googleapis.com`, `artifactregistry.googleapis.com`
2. 워커용 서비스 계정에 역할 부여: **Cloud Run 관리자**(`roles/run.admin`), **서비스 계정 관리자**(`roles/iam.serviceAccountAdmin`, 앱 전용 계정 생성), **서비스 계정 사용자**(`roles/iam.serviceAccountUser`, 앱 계정으로 실행)
   그리고 이미지 저장소 하나에 대해 **Artifact Registry 리더**(`roles/artifactregistry.reader`). Cloud Run 은 서비스를 만드는 계정에게 이미지 읽기 권한을 요구한다 (없으면 apply 에서 `artifactregistry.repositories.downloadArtifacts` 403):
   `gcloud artifacts repositories add-iam-policy-binding <저장소> --location <리전> --member serviceAccount:<워커 계정> --role roles/artifactregistry.reader`
3. 콘솔 → IAM 및 관리자 → 서비스 계정 → 해당 계정 → **키** → 키 추가 → JSON → 내려받기
4. 워커를 실행하는 곳에서 경로 지정:
   ```powershell
   $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\keys\pawploy-worker.json"   # PowerShell
   ```

- 이미지는 **같은 프로젝트의 Artifact Registry** 에 있어야 합니다 (CodeBuild 가 ECR 과 함께 푸시).
- Cloud Run 공개 접속은 `allUsers` 에게 호출 권한을 줍니다. 조직 정책(도메인 제한 공유)이 막으면 apply 에서 실패합니다.
- GCP 배포의 state 도 S3 backend(`PAWPLOY_STATE_BUCKET`)에 둡니다. 워커는 AWS 에서 돕니다.

---

## 환경변수

| 이름 | 설명 |
|---|---|
| `PAWPLOY_STATE_BUCKET` | 지정하면 state를 S3에 저장 (`deployments/<project_id>/<deploy_id>/<cloud>.tfstate`, S3 네이티브 잠금). 없으면 작업 폴더에 로컬 저장 |
| `PAWPLOY_STATE_REGION` | state 버킷 리전 (기본: AWS 는 배포 리전, GCP 는 `PAWPLOY_REGION`) |
| `PAWPLOY_ARTIFACT_BUCKET` | 지정하면 작업 폴더를 `workdirs/<deploy_id>/<cloud>/`에 보관. 로컬에 없으면 destroy 때 여기서 받음 |
| `PAWPLOY_AGENT_BUCKET` | AgentCore 버킷. `terraform_uri` 가 없으면 `s3://<버킷>/projects/<project_id>/deploy/<deploy_id>/attempt-<N>/` 에서 **N 이 가장 큰** 폴더의 모듈을 씀(숫자 비교). `attempt-N/<cloud>/` 가 있으면 그 폴더. 하나도 없으면 기본 모듈. 쓴 위치는 결과의 `targets.<cloud>.terraform_source` |
| `PAWPLOY_STATUS_TABLE` | 지정하면 결과를 DynamoDB 에도 기록 (파티션 키 `deploy_id`). `sweep` 이 다른 머신의 배포도 찾는 근거 |
| `PAWPLOY_STATUS_REGION` | 상태 테이블 리전 (기본 `PAWPLOY_REGION`) |
| `PAWPLOY_DESTROY_QUEUE_ARN`, `PAWPLOY_SCHEDULER_ROLE_ARN` | 둘 다 있으면 AWS target 에 EventBridge Scheduler 예약을 함께 만듦 |
| `PAWPLOY_DESTROY_QUEUE_URL` | `drain-destroy-queue` 가 읽을 큐 주소 |
| `PAWPLOY_LOCK_LEASE_SEC` | DynamoDB 배포 잠금 임대 시간(초, 기본 3600). 워커가 죽어도 이 시간 뒤 잠금이 풀림 |
| `PAWPLOY_REGION` | `sweep`·`orphans`·상태 테이블 기본 리전 (기본 `ap-northeast-2`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP 서비스 계정 키 파일 경로 |
| `PAWPLOY_WORK_DIR` | 작업 폴더 위치 (기본 `./work`) |
| `PAWPLOY_OFFLINE` | `1`이면 AWS 호출 단계를 건너뜀 (시험용) |
| `PAWPLOY_HEALTH_TIMEOUT`, `PAWPLOY_HEALTH_INTERVAL` | 헬스체크 최대 대기·간격(초). 기본 EC2 420 / Lambda·Cloud Run 180, 간격 10 |
| `TERRAFORM_BIN`, `AWS_BIN` | 실행 파일 경로 |

**동시 실행 방지**: `PAWPLOY_STATUS_TABLE` 이 있으면 deploy·destroy 는 `deploy_id` 단위 잠금(같은 테이블의 `lock#<deploy_id>` 항목, 조건부 쓰기)을 잡고,
다른 워커가 DynamoDB 에 남긴 결과를 이어받은 뒤 진행합니다. 잠금을 못 잡으면 deploy 는 종료 코드 2, destroy 는 1(다음 점검에서 재시도)입니다.
SQS 중복 메시지, sweep·사용자 종료·재시도가 겹치는 경우를 막습니다.

**다른 머신·컨테이너에서 destroy하려면** `PAWPLOY_STATE_BUCKET`과 `PAWPLOY_ARTIFACT_BUCKET`이 둘 다 필요합니다.
state를 찾을 수 없으면 destroy를 거부하고 `destroy_failed`를 기록합니다. state 없이 destroy하면 terraform은 "지울 것 없음"으로 성공해 버리고, 실제 리소스는 남기 때문입니다.

---

## 1시간 자동 삭제

우리 계정에 배포하므로 **지워지는 것이 배포되는 것보다 중요**합니다. 한 겹이 빠져도 다음 겹이 잡도록 세 겹으로 둡니다.

| 겹 | 무엇이 | 언제 | 켜는 조건 |
|---|---|---|---|
| 1. 예약 | EventBridge Scheduler 가 SQS 큐로 `{"action":"destroy","deploy_id":...}` 전송 (AWS target 에만 생성, 메시지는 배포 전체 삭제 요청) → `drain-destroy-queue` 가 받아 destroy | 정확히 `expires_at` (+ 큐 처리 주기) | `PAWPLOY_DESTROY_QUEUE_ARN` + `PAWPLOY_SCHEDULER_ROLE_ARN`, 처리: `PAWPLOY_DESTROY_QUEUE_URL` |
| 2. 정기 점검 | `sweep` 이 로컬 `work/` 와 DynamoDB 에서 만료됐는데 `destroyed`/`failed` 가 아닌 배포(`partial` 포함)를 찾아 **모든 클라우드** destroy | 5~10분마다 | 항상 |
| 3. 태그 감시 | `orphans` 가 `pawploy:expires_at` 태그가 지난 **AWS** 리소스를 목록으로 출력 (삭제 안 함) | 하루 몇 번 | 항상. GCP label 감시는 아직 없음 |

- GCP 만 배포한 경우 1번 예약이 없으므로 2번 `sweep` 이 지웁니다.
- `sweep` 은 `deploying`·`destroying` 처럼 다른 프로세스가 작업 중일 수 있는 상태는 만료 15분 뒤까지 건너뜁니다.

한 번만 만들어 두는 것 (팀 AWS 계정): S3 버킷(state·작업 폴더, 비공개·암호화·버전 관리), DynamoDB 테이블, SQS 큐, Scheduler 역할.

```bash
bash infra/setup-aws.sh            # 무엇을 만들지 출력만 (이미 있는 것은 건너뜀)
bash infra/setup-aws.sh --apply    # 실제로 생성 → 마지막에 워커 환경변수 값을 출력
```

---

## 폴더 구조

```
tfworker/          워커 (위 표 참고) + awscli.py(aws CLI 실행 도우미)
modules/           기본 모듈 = AgentCore 가 고칠 베이스 (terraform_uri 가 없으면 그대로 사용)
  ec2/             Amazon Linux 2023 + Docker. 기본 VPC, 80번 포트만, ECR 읽기 권한만
  lambda/          이미지 Lambda + 인증 없는 함수 URL, 로그 쓰기 권한만
  cloud_run/       Cloud Run v2 + 권한 없는 앱 전용 서비스 계정 + 공개 호출, 인스턴스 최대 1개
examples/          작업 입력 예시 (job-ec2 / job-gcp / job-multi), 테스트용 sample-app
tools/             fake-terraform.py, fake-aws.py (테스트용), push-sample-image.sh
infra/setup-aws.sh 운영 인프라(S3·DynamoDB·SQS·Scheduler 역할) 생성 스크립트
Dockerfile         워커 실행 이미지
tests/             unittest (python -m unittest -v)
work/<deploy_id>/  배포마다 생기는 작업 폴더 (git 제외): result.json + aws/ gcp/
```

---

## 검증 상태

| 항목 | 상태 |
|---|---|
| 가짜 terraform·aws 로 36개 경로 (+ 기존 역할·계정 재사용 차단, S3 작업 입력, maintenance) (+ 입력 오류 기록, DynamoDB 잠금·결과 이어받기, 만료 큐 처리): 단일·멀티 클라우드, 한쪽 실패 후 그쪽만 재시도, 단계별 실패 보고·정리, AgentCore 모듈 사용·IaC 거부·정책 거부, 입력 오류, sweep·orphans 등 | ✅ |
| 실제 terraform `validate` (EC2·Lambda·Cloud Run 루트+모듈) + `fmt` | ✅ (AWS provider 6.67, Google provider 6.50) |
| 실제 AWS EC2 한 바퀴 (배포 → 접속 → 삭제) | ✅ 2026-10-02 (`targets` 구조로 재확인: 헬스체크 121초, 삭제 후 남은 리소스 없음) |
| 실제 GCP Cloud Run 한 바퀴 (배포 → 접속 → 삭제) | ✅ 2026-10-02 (`softbankhackathon2026-peony`, 첫 시도 403 → 실패 보고·정리 → 권한 추가 후 같은 deploy_id 재시도 성공) |
| 실제 AWS·GCP 동시 배포, S3·DynamoDB·Scheduler 실제 호출 | ❌ |

## 다음 할 일

1. 바뀐 구조로 EC2 한 바퀴 다시 확인 → GCP 서비스 계정 키 준비 → Cloud Run 한 바퀴 → AWS+GCP 동시 한 바퀴
2. AgentCore 담당과 모듈 약속(위 표) 확정, Build Worker 담당과 Artifact Registry 푸시·digest 전달 확정
3. Main Server 와 연결 방식(호출·결과 전달) 결정 — destroy 큐 소비자도 함께
4. GCP label 기반 남은 리소스 감시 (`orphans` 의 GCP 판)

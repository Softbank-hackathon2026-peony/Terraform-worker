# Pawploy Terraform Worker

ECR 이미지 주소와 AgentCore의 추천 결과를 받아 **팀 AWS 계정에 앱을 배포하고 접속 주소를 돌려주는** 워커입니다.
Pawploy 전체 흐름에서 ⑦ 배포를 맡습니다.

- 지원 아키텍처: `ec2`, `lambda` (ECS Fargate는 예정)
- 배포는 최대 60분 뒤 만료됩니다. 만료 정리는 예약(EventBridge Scheduler) + 정기 점검(`sweep`) + 태그 감시(`orphans`) 세 겹으로 합니다 → [1시간 자동 삭제](#1시간-자동-삭제)
- apply 전에 plan 을 정책(허용 리소스·크기·태그)으로 검사해 허용 밖 변경을 막습니다
- Python 표준 라이브러리만 사용합니다. AWS 호출은 `terraform`과 `aws` CLI로 합니다

---

## 설계: AI는 앞과 뒤에서만, 실행은 정해진 코드로

```
 ┌───────────────────────┐
 │ AgentCore 분석·추천    │  사용자 소스를 읽고 아키텍처·포트·크기를 골라 S3에 JSON 저장
 └──────────┬────────────┘
            ▼
 ┌─────────────────────────── Terraform Worker (AI 없음) ────────────────────────────┐
 │ ② 추천 결과 읽기 → 입력 검증 → ① ECR 이미지 확인 → ③ Terraform 생성                │
 │                   → ④ S3 보관 → ⑤ plan → 정책 검사 → apply → 헬스체크              │
 │                   상태는 result.json + DynamoDB 에, 만료되면 sweep 이 destroy       │
 └──────────┬────────────────────────────────────────────────────────────────────────┘
            ▼ (failed / unhealthy일 때만)
 ┌───────────────────────┐
 │ AgentCore 진단         │  로그를 읽고 실패 원인을 쉬운 말로 설명 (읽기 권한만)
 └───────────────────────┘
```

| 단계 | 파일 | 하는 일 |
|---|---|---|
| ② 추천 결과 읽기 | `tfworker/recommendation.py` | `recommendation_uri`(S3 또는 로컬 파일)를 읽어 작업 입력에 합침 |
| 입력 검증 | `tfworker/job.py` | 허용 아키텍처·크기, ECR 주소 형식, 환경변수 이름, TTL 최대 60분 |
| ① ECR 이미지 확인 | `tfworker/image.py` | 이미지가 ECR에 있는지 확인하고 태그를 `@sha256:` digest로 고정 |
| ③ Terraform 생성 | `tfworker/render.py` | `work/<deploy_id>/`에 `main.tf`, 변수 파일, 모듈 복사본 생성 |
| ④ S3 보관 | `tfworker/artifacts.py` | 작업 폴더를 `s3://<버킷>/workdirs/<deploy_id>/`에 저장 |
| ⑤ 적용 | `tfworker/terraform.py` | `init → plan → show -json → apply → output` |
| 정책 검사 | `tfworker/policy.py` | plan 에 허용 밖 리소스 종류·인스턴스 타입·Lambda 크기·열린 포트·태그 누락이 있으면 apply 하지 않음 |
| 헬스체크 | `tfworker/health.py` | 앱이 응답할 때까지 대기 (EC2 420초, Lambda 180초) |
| 진단 | `tfworker/diagnose.py` | 로그 수집 → AgentCore 진단 에이전트 호출 |
| 상태 기록 | `tfworker/store.py` | `result.json` 과 같은 내용을 DynamoDB 에도 기록 (Main Server 가 읽음) |
| 만료 정리 | `tfworker/expire.py` | 만료된 배포 찾기(`sweep`), 태그로 남은 리소스 찾기(`orphans`) |

**왜 ①~⑤에 AI를 쓰지 않나요?**
- 다섯 단계 모두 정답이 하나로 정해진 작업입니다. AI가 끼면 결과가 매번 달라질 수 있고, 미리 테스트할 수도 없습니다.
- 분석 에이전트는 사용자 코드를 읽습니다. 그 코드에 숨은 지시(프롬프트 인젝션)가 있을 수 있으므로, 에이전트는 **JSON만 내놓고** 워커가 허용 목록으로 검사한 뒤 정해진 모듈만 실행합니다.
- 진단 에이전트에는 로그만 넘기고 배포 권한은 주지 않습니다.

---

## 준비

| 도구 | 확인 명령 |
|---|---|
| Python 3.10 이상 | `py --version` (macOS 기본 python3 는 3.9 라 Homebrew python3.11 등을 쓸 것) |
| Terraform 1.10 이상 | `terraform -version` (S3 state 잠금 `use_lockfile` 이 1.10 부터) |
| AWS CLI v2 + 로그인 | `aws sts get-caller-identity` |

## 사용법

```bash
python -m tfworker deploy examples/job-ec2.json    # 배포
python -m tfworker status dep-demo-ec2             # 상태 보기 (work/<deploy_id>/result.json)
python -m tfworker destroy dep-demo-ec2            # 삭제
python -m tfworker sweep [--dry-run]               # 만료된 배포를 모두 삭제 (5~10분마다 정기 실행)
python -m tfworker orphans [region]                # 태그로 만료 지난 리소스 찾기 (삭제 안 함, 있으면 exit 1)
```

> Windows에서 `python`이 동작하지 않으면 `py -m tfworker ...`로 실행하세요.

종료 코드: `0` 성공 · `1` 배포·삭제 실패(정책 위반 포함) · `2` 입력 오류(이미지 없음 포함) · `3` 배포는 됐지만 응답 없음

### 비용 없이 흐름만 시험하기

`PAWPLOY_OFFLINE=1`은 AWS를 부르는 단계(이미지 확인, S3 보관, 상태 기록, 로그 수집)를 건너뜁니다.
`TERRAFORM_BIN`에 가짜 terraform(`tools/fake-terraform.py`)을 지정하면 실제 리소스 없이 deploy → 헬스체크 → destroy 흐름을 확인할 수 있습니다.
`AWS_BIN`에 가짜 aws(`tools/fake-aws.py`)를 지정하면 ECR 확인·S3 보관·DynamoDB 기록·태그 조회가 어떤 인자로 호출되는지 기록해 검증할 수 있습니다.

```bash
# 자동 테스트 19개 (정상 / apply·init 실패 / 응답 없음 / 입력 오류 / 정책 위반 / 재배포 거부 / state 없는 destroy 거부 /
#              추천 결과 병합 / ECR digest 고정 / DynamoDB 기록 / sweep·orphans / 스케줄러 렌더)
python -m unittest -v

# 손으로 한 번 돌려 보기 (헬스체크는 127.0.0.1:9 로 가서 몇 초 뒤 unhealthy 로 끝남)
PAWPLOY_OFFLINE=1 TERRAFORM_BIN=tools/fake-terraform.py PAWPLOY_HEALTH_TIMEOUT=5 PAWPLOY_HEALTH_INTERVAL=1 \
  python -m tfworker deploy examples/job-ec2.json
```

가짜 terraform 은 `FAKE_TF_FAIL=apply` 처럼 실패시킬 단계를, `FAKE_TF_ENDPOINT` 로 헬스체크 대상 주소를 바꿀 수 있습니다.

실제 terraform 으로 문법만 검사하려면 (provider 다운로드만 하고 AWS 는 부르지 않음):

```bash
terraform -chdir=work/<deploy_id> init -backend=false && terraform -chdir=work/<deploy_id> validate
```

---

## 입력 형식

### 작업 입력 (Main Server → Worker)

```json
{
  "deploy_id": "dep-demo-ec2",
  "project_id": "prj_demo",
  "image_uri": "<12자리계정>.dkr.ecr.ap-northeast-2.amazonaws.com/<저장소>:<태그 또는 @sha256:...>",
  "recommendation_uri": "s3://<버킷>/<경로>/recommendation.json",
  "architecture": "ec2",
  "container_port": 8080,
  "size": "small",
  "health_path": "/",
  "env": { "APP_MODE": "test" }
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `deploy_id` | ✅ | 소문자·숫자·하이픈 4~40자. AWS 리소스 이름 `pawploy-<deploy_id>`에 쓰임. **배포마다 새 값**을 써야 하며, 살아 있는 배포(`destroyed`/`failed`가 아닌 상태)와 같은 id 는 거부됨(종료 코드 2) |
| `project_id` | ✅ | 태그와 state 경로에 쓰임 |
| `image_uri` | ✅ | ECR 주소. 워커가 digest로 고정함 |
| `recommendation_uri` | | AgentCore 추천 결과 위치. 있으면 아래 필드의 기본값으로 쓰임 |
| `architecture` | ✅* | `ec2` 또는 `lambda` (*추천 결과에 있으면 생략 가능) |
| `container_port` | | 기본 8080 |
| `size` | | `micro` / `small` / `medium`, 기본 `small` |
| `health_path` | | 기본 `/` |
| `env` | | 환경변수. 추천 결과의 `env` 위에 덮어씀 |
| `region` | | 생략하면 이미지 주소에서 추출. Lambda는 이미지와 같은 리전이어야 함 |
| `ttl_minutes` | | 5~60, 기본 60 |

**합치는 규칙**: 추천 결과 → 작업 입력 순서로 덮어씁니다. Main Server가 직접 지정한 값이 이깁니다.

### AgentCore 추천 결과 (분석 에이전트 → S3) ⚠️ 잠정안

AgentCore 담당과 확정해야 합니다. 워커는 아래 다섯 필드만 읽고 나머지는 무시합니다.

```json
{
  "architecture": "ec2",
  "container_port": 8080,
  "size": "small",
  "health_path": "/",
  "env": { "APP_MODE": "test" },
  "reason": "상시 실행되는 웹 서버라서 EC2가 적합합니다"
}
```

예시: `examples/recommendation-ec2.json`, `examples/job-from-recommendation.json`

### AgentCore 진단 (Worker → 진단 에이전트) ⚠️ 잠정안

`status`가 `failed`나 `unhealthy`가 되면 워커가 `work/<deploy_id>/diagnosis_input.json`을 만듭니다.
`PAWPLOY_DIAGNOSE_AGENT_ARN`이 있으면 이 파일을 AgentCore Runtime에 보내고, 응답을 `result.json`의 `diagnosis`에 넣습니다.

에이전트가 받는 값:

```json
{
  "deploy_id": "dep-demo-ec2",
  "architecture": "ec2",
  "status": "unhealthy",
  "error": null,
  "health_url": "http://1.2.3.4/",
  "job": { "container_port": 8080, "health_path": "/", "size": "small" },
  "env_names": ["APP_MODE"],
  "terraform_log_tail": "...",
  "app_log": "EC2 콘솔 출력 또는 Lambda CloudWatch Logs (최근 8000자)"
}
```

- 환경변수는 **이름만** 보내고 값은 보내지 않습니다.
- `app_log`는 리소스가 만들어진 경우(`unhealthy`)에만 들어갑니다.

에이전트가 돌려줄 값(제안):

```json
{
  "summary": "앱이 3000번 포트에서 실행 중인데 8080으로 배포되었습니다.",
  "likely_cause": "port_mismatch",
  "suggestions": ["container_port를 3000으로 바꿔 다시 배포해 보세요"]
}
```

JSON이 아니면 전체 응답 텍스트를 `{"summary": "..."}`로 저장합니다. 수정 제안을 반영한 재배포도 새 작업 입력으로 같은 검증을 거칩니다.

---

## 상태 흐름 (`result.json`의 `status`)

```
preparing → generating → init → plan → (정책 검사) → apply → health_check → running
                          │       │                     │                └→ unhealthy (진단)
                          │       └→ failed (리소스 없음, 정리 생략)       └→ failed (진단 → 자동 destroy 시도)
                          └→ failed (리소스 없음, 정리 생략)
destroying → destroyed  /  destroy_failed
```

④ S3 보관은 `init` 단계 안에서 실행합니다. init 뒤에 보관해야 provider 잠금 파일(`.terraform.lock.hcl`)까지 함께 저장됩니다.
apply 전(init·plan·정책 검사)에 실패하면 만들어진 리소스가 없으므로 destroy 를 돌리지 않고 `failed` 로 끝냅니다. 같은 `deploy_id` 를 바로 다시 쓸 수 있습니다.
`PAWPLOY_STATUS_TABLE` 이 있으면 상태가 바뀔 때마다 같은 내용이 DynamoDB 에도 기록됩니다.

---

## 환경변수

| 이름 | 설명 |
|---|---|
| `PAWPLOY_STATE_BUCKET` | 지정하면 state를 S3에 저장 (`deployments/<project_id>/<deploy_id>.tfstate`, S3 네이티브 잠금 `use_lockfile=true`). 없으면 작업 폴더에 로컬 저장 |
| `PAWPLOY_STATE_REGION` | state 버킷 리전 (기본: 배포 리전) |
| `PAWPLOY_ARTIFACT_BUCKET` | 지정하면 작업 폴더를 `workdirs/<deploy_id>/`에 보관. 로컬에 없으면 destroy 때 여기서 받음 |
| `PAWPLOY_DIAGNOSE_AGENT_ARN` | AgentCore 진단 에이전트 Runtime ARN. 없으면 진단 자료만 저장 |
| `PAWPLOY_STATUS_TABLE` | 지정하면 상태를 DynamoDB 에도 기록 (파티션 키 `deploy_id`). `sweep` 이 다른 머신의 배포도 찾는 근거 |
| `PAWPLOY_STATUS_REGION` | 상태 테이블 리전 (기본: 배포 리전) |
| `PAWPLOY_DESTROY_QUEUE_ARN`, `PAWPLOY_SCHEDULER_ROLE_ARN` | 둘 다 있으면 배포마다 EventBridge Scheduler 가 `expires_at` 에 이 SQS 큐로 destroy 요청을 보냄 |
| `PAWPLOY_REGION` | `sweep`(DynamoDB 조회)·`orphans` 기본 리전 (기본 `ap-northeast-2`) |
| `PAWPLOY_WORK_DIR` | 작업 폴더 위치 (기본 `./work`) |
| `PAWPLOY_OFFLINE` | `1`이면 AWS 호출 단계를 건너뜀 (시험용) |
| `PAWPLOY_HEALTH_TIMEOUT`, `PAWPLOY_HEALTH_INTERVAL` | 헬스체크 최대 대기·재시도 간격(초). 기본 EC2 420 / Lambda 180, 간격 10. 테스트에서 짧게 줄일 때만 사용 |
| `TERRAFORM_BIN`, `AWS_BIN` | 실행 파일 경로 |

**다른 머신·컨테이너에서 destroy하려면** `PAWPLOY_STATE_BUCKET`과 `PAWPLOY_ARTIFACT_BUCKET`이 둘 다 필요합니다.
state를 찾을 수 없으면 워커가 destroy를 거부하고 `destroy_failed`를 기록합니다. state 없이 destroy하면 terraform은 "지울 것 없음"으로 성공해 버리고, 실제 리소스는 남기 때문입니다.

---

## 1시간 자동 삭제

우리 계정에 배포하므로 **지워지는 것이 배포되는 것보다 중요**합니다. 한 겹이 빠져도 다음 겹이 잡도록 세 겹으로 둡니다.

| 겹 | 무엇이 | 언제 | 켜는 조건 |
|---|---|---|---|
| 1. 예약 | EventBridge Scheduler 가 SQS 큐로 `{"action":"destroy","deploy_id":...}` 전송. 한 번 실행 뒤 스케줄 자동 삭제 | 정확히 `expires_at` | `PAWPLOY_DESTROY_QUEUE_ARN` + `PAWPLOY_SCHEDULER_ROLE_ARN` |
| 2. 정기 점검 | `python -m tfworker sweep` 이 로컬 `work/` 와 DynamoDB 상태 테이블에서 만료됐는데 `destroyed`/`failed` 가 아닌 배포를 찾아 destroy | 5~10분마다 (크론·스케줄러) | 항상. 다른 머신 배포까지 보려면 `PAWPLOY_STATUS_TABLE` |
| 3. 태그 감시 | `python -m tfworker orphans` 가 `pawploy:managed` 태그 리소스 중 `pawploy:expires_at` 이 지난 것을 목록으로 출력. **삭제하지 않음**, 있으면 exit 1 | 하루 몇 번 (알림 조건) | 항상 |

- `sweep` 은 `preparing`~`health_check`·`destroying` 처럼 다른 프로세스가 작업 중일 수 있는 상태는 만료 15분 뒤까지 건너뜁니다.
- 1번 큐의 메시지를 받아 `python -m tfworker destroy <deploy_id>` 를 실행하는 쪽은 Main Server 연결 방식(SQS 소비 / ECS 작업 등)과 함께 정합니다. 그 전까지는 2번만으로도 지워집니다.
- IAM 역할처럼 리전이 없는 리소스는 `orphans us-east-1` 로 조회해야 나옵니다.

한 번만 만들어 두는 것 (팀 계정):

```bash
# 상태 테이블
aws dynamodb create-table --table-name pawploy-deployments \
  --attribute-definitions AttributeName=deploy_id,AttributeType=S \
  --key-schema AttributeName=deploy_id,KeyType=HASH --billing-mode PAY_PER_REQUEST

# destroy 요청 큐 + Scheduler 가 그 큐에 보낼 수 있는 역할
aws sqs create-queue --queue-name pawploy-destroy
aws iam create-role --role-name pawploy-scheduler --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam put-role-policy --role-name pawploy-scheduler --policy-name send-destroy --policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"sqs:SendMessage","Resource":"arn:aws:sqs:ap-northeast-2:<계정>:pawploy-destroy"}]}'
```

---

## 폴더 구조

```
tfworker/          워커 (위 표 참고) + awscli.py(aws CLI 실행 도우미)
modules/
  ec2/             Amazon Linux 2023 + Docker. 기본 VPC, 80번 포트만 개방, ECR 읽기 권한만
  lambda/          이미지 Lambda + 인증 없는 함수 URL(InvokeFunctionUrl + InvokeFunction 두 권한), 로그 쓰기 권한만
examples/          작업 입력·추천 결과 예시, 테스트용 sample-app (Lambda Web Adapter 1.1.0)
tools/
  fake-terraform.py      가짜 terraform (비용 없는 흐름 시험용, show -json 으로 정책 검사까지)
  fake-aws.py            가짜 aws CLI (호출 기록 + 정해진 응답, 테스트용)
  push-sample-image.sh   샘플 이미지를 linux/amd64 로 빌드해 ECR 에 푸시 (실제 배포 준비)
tests/             unittest (python -m unittest -v)
work/<deploy_id>/  배포마다 생기는 작업 폴더 (git 제외)
```

모든 모듈은 같은 입력(`name`, `image_uri`, `container_port`, `size`, `env`, `health_path`)과 같은 출력(`endpoint`, `health_url`, `resource_id`)을 가집니다.
모든 리소스에는 `pawploy:managed`, `pawploy:project_id`, `pawploy:deploy_id`, `pawploy:expires_at` 태그가 붙습니다.

---

## 검증 상태

| 항목 | 상태 |
|---|---|
| 가짜 terraform·aws 로 19개 경로 (`tests/`): 정상 / apply·init 실패 / 응답 없음 / 입력 오류 / 정책 위반 / 재배포 거부 / state 없는 destroy / 추천 병합 / digest 고정 / DynamoDB 기록 / sweep·orphans / 스케줄러 렌더 | ✅ |
| 실제 terraform `validate` (EC2, Lambda, 스케줄러 포함 루트) + `fmt` | ✅ (2026-10-02, Terraform 1.16.4 + AWS provider 6.67.0) |
| 실제 AWS 배포 (EC2, Lambda) | ❌ 아직 → `tools/push-sample-image.sh` 로 이미지 올린 뒤 EC2 한 바퀴부터 |
| ECR digest 고정, S3 보관·복원, DynamoDB 기록, 진단 로그 수집, EventBridge Scheduler 예약 | ❌ 가짜 aws 로 호출 인자만 검증. 실제 AWS 로 아직 안 해 봄 |
| AgentCore 진단 에이전트 호출 (`aws bedrock-agentcore invoke-agent-runtime`) | ❌ 에이전트가 아직 없음 |

## 다음 할 일

1. `tools/push-sample-image.sh` 로 샘플 이미지를 ECR 에 올리고 EC2 한 바퀴(배포 → 접속 → 삭제) 성공시키기
2. Lambda 한 바퀴
3. 상태 테이블·destroy 큐·Scheduler 역할을 팀 계정에 만들고 실제로 예약 → 만료 → 삭제 확인, `sweep` 을 5~10분 주기로 돌릴 자리 정하기
4. Main Server 와 연결 방식 결정 (SQS 소비 / ECS 작업 등) — destroy 큐 소비자도 여기서 함께
5. AgentCore 담당과 추천 결과·진단 형식 확정, 진단 에이전트 만들기
6. ECS Fargate 모듈 (공용 ALB + 배포별 대상 그룹·리스너 규칙)

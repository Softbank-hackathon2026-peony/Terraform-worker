# Pawploy Terraform Worker

ECR 이미지 주소와 AgentCore의 추천 결과를 받아 **팀 AWS 계정에 앱을 배포하고 접속 주소를 돌려주는** 워커입니다.
Pawploy 전체 흐름에서 ⑦ 배포를 맡습니다.

- 지원 아키텍처: `ec2`, `lambda` (ECS Fargate는 예정)
- 배포는 최대 60분 뒤 만료됩니다(자동 삭제 예약은 아직 구현 전)
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
 │                                → ④ S3 보관 → ⑤ plan / apply → 헬스체크             │
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
| ⑤ 적용 | `tfworker/terraform.py` | `init → plan → apply → output` |
| 헬스체크 | `tfworker/health.py` | 앱이 응답할 때까지 대기 (EC2 420초, Lambda 180초) |
| 진단 | `tfworker/diagnose.py` | 로그 수집 → AgentCore 진단 에이전트 호출 |

**왜 ①~⑤에 AI를 쓰지 않나요?**
- 다섯 단계 모두 정답이 하나로 정해진 작업입니다. AI가 끼면 결과가 매번 달라질 수 있고, 미리 테스트할 수도 없습니다.
- 분석 에이전트는 사용자 코드를 읽습니다. 그 코드에 숨은 지시(프롬프트 인젝션)가 있을 수 있으므로, 에이전트는 **JSON만 내놓고** 워커가 허용 목록으로 검사한 뒤 정해진 모듈만 실행합니다.
- 진단 에이전트에는 로그만 넘기고 배포 권한은 주지 않습니다.

---

## 준비

| 도구 | 확인 명령 |
|---|---|
| Python 3.10 이상 | `py --version` |
| Terraform 1.5 이상 | `terraform -version` |
| AWS CLI v2 + 로그인 | `aws sts get-caller-identity` |

## 사용법

```bash
python -m tfworker deploy examples/job-ec2.json    # 배포
python -m tfworker status dep-demo-ec2             # 상태 보기 (work/<deploy_id>/result.json)
python -m tfworker destroy dep-demo-ec2            # 삭제
```

> Windows에서 `python`이 동작하지 않으면 `py -m tfworker ...`로 실행하세요.

종료 코드: `0` 성공 · `1` 배포·삭제 실패 · `2` 입력 오류(이미지 없음 포함) · `3` 배포는 됐지만 응답 없음

### 비용 없이 흐름만 시험하기

`PAWPLOY_OFFLINE=1`은 AWS를 부르는 단계(이미지 확인, S3 보관, 로그 수집)를 건너뜁니다.
`TERRAFORM_BIN`에 가짜 terraform을 지정하면 실제 리소스 없이 deploy → 헬스체크 → destroy 흐름을 확인할 수 있습니다.

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
| `deploy_id` | ✅ | 소문자·숫자·하이픈 4~40자. AWS 리소스 이름 `pawploy-<deploy_id>`에 쓰임 |
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
preparing → generating → init → plan → apply → health_check → running
                                   │                       └→ unhealthy (진단)
                                   └→ failed (진단 → 자동 destroy 시도)
destroying → destroyed  /  destroy_failed
```

④ S3 보관은 `init` 단계 안에서 실행합니다. init 뒤에 보관해야 provider 잠금 파일(`.terraform.lock.hcl`)까지 함께 저장됩니다.

---

## 환경변수

| 이름 | 설명 |
|---|---|
| `PAWPLOY_STATE_BUCKET` | 지정하면 state를 S3에 저장 (`deployments/<project_id>/<deploy_id>.tfstate`). 없으면 작업 폴더에 로컬 저장 |
| `PAWPLOY_STATE_REGION` | state 버킷 리전 (기본: 배포 리전) |
| `PAWPLOY_ARTIFACT_BUCKET` | 지정하면 작업 폴더를 `workdirs/<deploy_id>/`에 보관. 로컬에 없으면 destroy 때 여기서 받음 |
| `PAWPLOY_DIAGNOSE_AGENT_ARN` | AgentCore 진단 에이전트 Runtime ARN. 없으면 진단 자료만 저장 |
| `PAWPLOY_WORK_DIR` | 작업 폴더 위치 (기본 `./work`) |
| `PAWPLOY_OFFLINE` | `1`이면 AWS 호출 단계를 건너뜀 (시험용) |
| `TERRAFORM_BIN`, `AWS_BIN` | 실행 파일 경로 |

**다른 머신·컨테이너에서 destroy하려면** `PAWPLOY_STATE_BUCKET`과 `PAWPLOY_ARTIFACT_BUCKET`이 둘 다 필요합니다.
state를 찾을 수 없으면 워커가 destroy를 거부하고 `destroy_failed`를 기록합니다. state 없이 destroy하면 terraform은 "지울 것 없음"으로 성공해 버리고, 실제 리소스는 남기 때문입니다.

---

## 폴더 구조

```
tfworker/          워커 (위 표 참고) + awscli.py(aws CLI 실행 도우미)
modules/
  ec2/             Amazon Linux 2023 + Docker. 기본 VPC, 80번 포트만 개방, ECR 읽기 권한만
  lambda/          이미지 Lambda + 인증 없는 함수 URL, 로그 쓰기 권한만
examples/          작업 입력·추천 결과 예시, 테스트용 sample-app
work/<deploy_id>/  배포마다 생기는 작업 폴더 (git 제외)
```

모든 모듈은 같은 입력(`name`, `image_uri`, `container_port`, `size`, `env`, `health_path`)과 같은 출력(`endpoint`, `health_url`, `resource_id`)을 가집니다.
모든 리소스에는 `pawploy:managed`, `pawploy:project_id`, `pawploy:deploy_id`, `pawploy:expires_at` 태그가 붙습니다.

---

## 검증 상태

| 항목 | 상태 |
|---|---|
| 가짜 terraform으로 정상 / apply 실패 / 응답 없음 / 입력 오류 / state 없는 destroy 경로 | ✅ |
| 실제 terraform `validate` (EC2, Lambda 모듈, AWS provider 5.100.0) | ✅ |
| 실제 AWS 배포 (EC2, Lambda) | ❌ 아직 |
| ECR digest 고정, S3 보관·복원, 진단 로그 수집 | ❌ 실제 AWS로 아직 안 해 봄 |
| AgentCore 진단 에이전트 호출 (`aws bedrock-agentcore invoke-agent-runtime`) | ❌ 에이전트가 아직 없음 |

## 다음 할 일

1. 샘플 이미지를 `linux/amd64`로 빌드해 ECR에 푸시하고 EC2 한 바퀴(배포 → 접속 → 삭제) 성공시키기
2. Lambda 한 바퀴
3. 1시간 자동 삭제 (EventBridge Scheduler + 정기 점검)
4. 상태 기록을 `result.json`에서 DynamoDB로 바꾸기
5. AgentCore 담당과 추천 결과·진단 형식 확정, 진단 에이전트 만들기

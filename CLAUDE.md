# CLAUDE.md: Pawploy Terraform Worker

claude.ai에서 나눈 설계 대화를 정리한 컨텍스트입니다. 작업 전에 끝까지 읽어 주세요.
(이전 문서에 있던 MBTI 추천, 3×3 매트릭스, 사용자 계정 배포 내용은 **모두 폐기**되었습니다.
멀티 클라우드는 2026-10-02 에 "사용자가 AWS·GCP 중 배포할 곳을 고른다(둘 다 가능)"로 다시 범위에 들어왔습니다.)

---

## 0. 사용자와 일하는 방식

- **모든 답변은 한국어로.**
- 사용자는 Terraform 초보입니다. 새 개념은 짧게 풀어서 설명하고, 명령 실행 전에 무엇을 하는지 한 줄로 말해 주세요.
- 개발 환경은 **Windows + VS Code**입니다. 셸 명령은 PowerShell 또는 Git Bash 기준으로 안내하세요.
- `terraform apply`/`destroy`, 클라우드 리소스 생성처럼 **비용이 생기는 명령은 실행 전에 반드시 확인**받으세요. `init`, `validate`, `plan`, `fmt`는 확인 없이 실행해도 됩니다.
- AWS 키, GCP 서비스 계정 키(JSON), `.env`, state 파일은 커밋하지 않습니다.
- **현재 최우선 목표: "일단 실제로 배포가 되는 것".** 기능을 늘리기보다 클라우드별 한 바퀴(배포 → 접속 → 삭제)를 먼저 성공시키세요.

---

## 1. 서비스 개요 (Pawploy)

- **해커톤 주제**: One Action, Infinite Clouds: 로컬 웹앱을 원터치로 클라우드에 배포
- **대상**: 인프라를 잘 모르는 초보 개발자(바이브 코더)
- **목적**: 실제 클라우드에서 정상 배포되는지 확인하는 **테스트 환경 제공**
- **클라우드**: 사용자가 승인 단계에서 **AWS·GCP 중 고른다 (둘 다 가능)**. 배포는 우리 팀 계정(AWS 계정 / GCP 프로젝트)에

### 파이프라인 (확정, 팀 시퀀스 다이어그램 기준)

```
01~04  사용자 → Main: GitHub URL → 커밋 SHA 고정 소스 → S3 스냅샷
05~08  Main → AgentCore: 분석 → Dockerfile·buildspec 생성 → 추천
09~15  (병렬) CodeBuild 사전 빌드 → ECR (+ GCP 선택 시 Artifact Registry) 푸시 → digest
16~17  (병렬) 사용자 검토: 추천안·이유·비용·권한 → 승인(클라우드 선택) 또는 수정 요청(05부터 반복)
18~20  Main → AgentCore: 승인된 클라우드·추천안·digest 로 Terraform 모듈 생성 → S3 저장
21~27  Main → Terraform Worker: IaC 검증·plan·apply → 실패 시 22(보고) → 23~25(AgentCore 수정) → 21 반복   ← 내 담당
       성공 시 26(URL 확인) → 27(상태·리소스·URL 전달)
28     Main → 사용자: 결과
```

### 확정된 결정

- **배포는 우리 팀 계정에 한다.** 사용자 계정 배포 아님
- **악용 방지를 위해 1시간 타임아웃** 후 자동 삭제. 사용자가 직접 종료도 가능
- 배포 대상: AWS EC2·EC2 Compose(컨테이너 여러 개)·Lambda (ECS Fargate 예정), GCP Cloud Run. SageMaker 는 범위 밖
- GCP 이미지는 **CodeBuild 가 ECR 과 Artifact Registry 양쪽에 푸시**하고 digest 를 넘긴다
- AWS·GCP 동시 배포 중 **한쪽만 실패하면 성공한 쪽은 유지**하고, 실패한 클라우드만 재시도

---

## 2. 내 담당: Terraform Worker (21~27단계)

> **입력**: 승인된 클라우드별 **이미지 digest** + AgentCore 가 S3 에 둔 **Terraform 모듈 위치**(`terraform_uri`)
> **출력**: 클라우드별 **접속 주소·상태**, 실패 시 **실패 단계·오류 로그·현재 상태**

### 설계 원칙 (2026-10-02 A안 확정)

- **Terraform 은 AgentCore 가 만든다(18~19). 워커는 AI 없이 검증하고 실행만 한다.** (한때 넣었던 워커 내부 AI 생성 `aigen.py` 와 기본 모듈 자동 대체는 제거함)
- 워커가 **루트 main.tf(provider·backend·필수 태그/label)를 직접 만들고**, 받은 코드는 `modules/app` 으로만 쓴다 → AgentCore 코드가 계정·state·태그를 바꿀 수 없음
- 실행 전 **IaC 정적 검사**(`iac.py`) + apply 전 **plan 정책 검사**(`policy.py`). 이 두 검사가 사용자 코드에서 온 프롬프트 인젝션에 대한 실제 방어선
- **실패하면 어느 단계든 만든 리소스를 지운 뒤 보고**한다 → 수정본 재요청(23~25 → 21)은 항상 빈 상태에서 시작. 정책의 "배포 중 삭제 금지"와도 충돌하지 않음
- 워커가 AgentCore 를 직접 부르지 않는다. 실패 보고(22)를 Main Server 가 AgentCore 에 넘긴다
- `modules/ec2`·`ec2_compose`·`lambda`·`cloud_run` 은 **AgentCore 가 고칠 베이스**이자 `terraform_uri` 가 없을 때 쓰는 기본값
- **모든 모듈은 같은 입력, 같은 출력**: 입력 `name`, `image_uri`, `container_port`, `size`, `env`, `health_path` / 출력 `endpoint`, `health_url`, `resource_id`
  예외 `ec2_compose`(여러 컨테이너): 입력 `name`, `images`(map), `size`, `health_path` + 모듈 안의 `compose.yaml.tftpl`. 출력은 같음
- **클라우드마다 작업 폴더·state 를 따로** 둔다(`work/<deploy_id>/<cloud>/`). 모듈까지 복사해 두므로 1시간 뒤 destroy 때 같은 코드로 정확히 지움
- 필수 태그: AWS `pawploy:managed`·`pawploy:project_id`·`pawploy:deploy_id`·`pawploy:expires_at` / GCP label 은 `:` 를 못 써서 `pawploy-managed` 등 하이픈 이름

### 작업 입력 (21단계) — 자세한 표는 README

```json
{
  "deploy_id": "dep-demo", "project_id": "prj_demo",
  "container_port": 8080, "size": "small", "health_path": "/", "env": { "APP_MODE": "test" },
  "targets": [
    { "cloud": "aws", "architecture": "ec2", "image_uri": "<ECR>@sha256:...", "terraform_uri": "s3://.../aws/" },
    { "cloud": "gcp", "architecture": "cloud_run", "image_uri": "<리전>-docker.pkg.dev/<프로젝트>/<저장소>/<이름>@sha256:...", "terraform_uri": "s3://.../gcp/" }
  ]
}
```

- **AgentCore 모듈 위치 (2026-10-02 AgentCore 담당과 확인)**: 버킷 `pawploy-agent-<계정>`, `projects/<project_id>/deploy/<deploy_id>/attempt-<N>/main.tf`. `terraform_uri` 가 없으면 워커가 `PAWPLOY_AGENT_BUCKET` 에서 **N 이 가장 큰 attempt** 를 고른다(숫자 비교, `attempt-N/<cloud>/` 가 있으면 그 폴더, 없으면 기본 모듈). 쓴 위치는 `targets.<cloud>.terraform_source` 로 결과·실패 보고에 남김
- **멀티 클라우드 (2026-10-03 AgentCore 와 확정)**: AgentCore 가 한 번에 `attempt-N/aws/main.tf`·`attempt-N/gcp/main.tf` 를 만들고, 한쪽만 고쳐도 새 attempt 에 두 클라우드를 다 둔다. Main 은 배포할 **클라우드만** 고른다(`targets[].cloud` + `image_uri`, `architecture` 생략). 워커가 모듈의 대표 리소스로 아키텍처를 판단(입력과 다르면 generating 실패). AgentCore 실제 예시 `dep-demo-2` attempt-2 로 S3 최신 attempt 선택 → 아키텍처 판단(aws=lambda, gcp=cloud_run) → IaC → 실제 plan → 정책 검사 통과 (apply 안 함)
- `targets` 없이 `architecture`·`image_uri` 를 최상위에 두면 AWS 하나 (이전 형식, `examples/job-ec2.json`)
- **여러 컨테이너 (2026-10-03, 팀 계약 `2026-10-03-multi-container-contract.md` 4절)**: AWS target 에 `image_uri` 대신 `images: {이미지 id: ECR 주소}` → `ec2_compose`(EC2 1대 + Docker Compose). 이미지마다 ECR 검사·digest 고정, 리전은 모두 같아야 함. 아키텍처 판단: `aws_instance` 모듈에 `compose.yaml.tftpl` 이 있으면 `ec2_compose`. 이미지 입력 모양(images/image_uri)과 모듈 아키텍처가 다르면 generating 실패. 템플릿 약속(`images`·`passwords` 값, 80번 진입, 금지 설정)은 README "ec2_compose 모듈 약속". 루트 main.tf 에 `hashicorp/random ~> 3.6` 을 이 아키텍처일 때만 추가 (컨테이너 1개 아키텍처의 루트는 그대로)
- GCP 이미지는 digest 필수(워커에 gcloud 없음). GCP 프로젝트·리전은 Artifact Registry 주소에서 추출
- 살아 있는 클라우드를 다시 보내면 거부(종료 코드 2). 재시도는 `failed`·`destroyed` 클라우드만, 만료 시각은 처음 것 유지

### 상태 (`work/<deploy_id>/result.json`, 같은 내용이 DynamoDB 에도)

- 클라우드별 `targets.<cloud>.status`: `preparing → generating → init → plan → apply → health_check → running`
  실패: 정리 후 `failed` (`failed_stage`·`error`·`log_tail`·`app_log`·`destroyed`·`current_state`) / 정리 실패: `destroy_failed`
  삭제: `destroying → destroyed`
- 전체 `status`: `deploying` / `running` / `partial` / `failed` / `destroying` / `destroyed` / `destroy_failed`
- 응답 없음(`unhealthy`)은 따로 두지 않는다. 앱 로그를 모은 뒤 정리하고 `failed_stage: health_check` 로 보고

---

## 3. 현재 코드 상태

```
tfworker/
  __main__.py        진입점: deploy / destroy [aws|gcp] / status / sweep / orphans. 클라우드별 순차 배포·실패 처리·결과 집계
  job.py             21단계 입력 검증 (targets, 클라우드·아키텍처, ECR / Artifact Registry digest, 크기, env, TTL)
  recommendation.py  (이전 형식용) recommendation_uri 를 읽어 작업 입력에 합침
  image.py           AWS: ECR 이미지 확인, 태그 → @sha256 digest 고정
  render.py          work/<deploy_id>/<cloud>/: 모듈 가져오기(terraform_uri: S3 / 로컬, 없으면 기본 모듈) + 루트 main.tf
                     (AWS: default_tags, PAWPLOY_DESTROY_QUEUE_ARN+SCHEDULER_ROLE_ARN 있으면 만료 예약 / GCP: default_labels)
  iac.py             IaC 정적 검사: 금지 문법, 허용 리소스·data 소스, file()/templatefile() 경로, 입출력 약속, IAM, 크레딧, deletion_protection
  terraform.py       terraform CLI 실행 (로그 실시간 출력, json / text 캡처)
  policy.py          plan(show -json) 검사: 리소스 종류·EC2 타입·크레딧·Lambda 크기·인바운드 80·IAM·Cloud Run 메모리/인스턴스/권한/삭제 보호·필수 태그/label
  artifacts.py       검사를 통과한 작업 폴더를 S3(PAWPLOY_ARTIFACT_BUCKET)에 보관, destroy 때 복원
  health.py          헬스체크 (EC2 420초, Lambda·Cloud Run 180초)
  diagnose.py        응답 없음일 때 지우기 전 앱 로그 수집 (EC2 콘솔 / Lambda 로그. Cloud Run 은 아직)
  store.py           result.json 을 DynamoDB(PAWPLOY_STATUS_TABLE)에도 기록. 실패해도 배포는 계속
  expire.py          sweep(로컬+DynamoDB 에서 만료 배포 찾아 destroy), orphans(AWS 태그로 남은 리소스 알림)
  awscli.py          aws CLI 실행 도우미 (PAWPLOY_OFFLINE=1이면 AWS 호출 단계 건너뜀)
  (store.py)         + deploy_id 단위 잠금(같은 테이블 lock#<id>, 조건부 쓰기, 임대 3600초)·결과 조회(get)
  (__main__.py)      + 입력 오류 기록(status=rejected / last_rejection), 다른 워커의 결과 이어받기, drain-destroy-queue,
                       작업 JSON 을 s3:// 경로로 받기, maintenance(만료 큐 + sweep 한 번에)
  (policy·iac)       + 앱 권한 탈취 차단: EC2 프로필·Lambda 역할·Cloud Run 서비스 계정은 이 배포에서 만든 것만
                       (AWS 는 plan 에서 값이 정해져 있으면 기존 것, GCP 는 만든 계정 이메일과 비교·미지정 거부), 만료 예약은 루트에서만
Dockerfile         워커 실행 이미지 (python 3.12 + terraform 1.16.0 체크섬 검증 + aws CLI, 사용자 worker, /work)
infra/setup-aws.sh S3 버킷·DynamoDB·SQS·Scheduler 역할(ppw-scheduler) 생성 (기본 출력만, --apply 로 생성)
infra/setup_fargate.py  위 기본 + SQS FIFO pawploy-jobs.fifo(+DLQ)·ECR·Secrets Manager(GCP 키)·로그·IAM(ppw-ecs-execution/ppw-worker-task)
                   ·ECS 클러스터 pawploy·서비스 pawploy-tf-worker(consume, 0.5vCPU/2GB 상시 1개). 이미지 태그 = git short SHA
tfworker/consume.py  작업 큐 소비자: {action:deploy, job_uri:s3://...} / {action:destroy, deploy_id, cloud?}, 5분마다 maintenance
modules/
  ec2/            Amazon Linux 2023 + Docker. 기본 VPC, 80번 포트만, ECR 읽기 권한, IMDSv2, 디스크 암호화, CPU 크레딧 standard
    user_data.sh.tftpl   부팅 시 Docker 설치 → ECR 로그인 → 이미지 실행 (-p 80:<container_port>)
  ec2_compose/    ec2 와 같은 보안 + IMDS 홉 1·디스크 30GB. random_password(템플릿의 passwords["id"] 마다 하나)
    user_data.sh.tftpl   Docker + Compose 플러그인(v2.39.4, 체크섬 고정) → 레지스트리별 ECR 로그인 → compose.yaml → pull → up -d → 상태·로그를 콘솔에
    compose.yaml.tftpl   예시 (app + postgres + redis). 실제로는 AgentCore 코드가 deploy_units 로 렌더해 넣음
  lambda/         이미지 Lambda + 인증 없는 함수 URL(InvokeFunctionUrl + InvokeFunction), 로그 쓰기 권한만
  cloud_run/      Cloud Run v2 + 권한 없는 앱 전용 서비스 계정 + allUsers 호출, 인스턴스 최대 1, deletion_protection=false
examples/
  job-ec2.json, job-lambda.json (이전 형식), job-gcp.json, job-multi.json, job-ec2-compose.json (targets 형식). image_uri 는 실제 값으로 바꿔 work/ 에 복사해 쓸 것
  sample-app/     테스트용 이미지 (Python 웹앱 + Lambda Web Adapter 1.1.0, EC2·Lambda·Cloud Run 겸용, PORT 환경변수 사용)
tools/
  fake-terraform.py     가짜 terraform (FAKE_TF_FAIL, FAKE_TF_FAIL_CLOUD, FAKE_TF_ENDPOINT, FAKE_TF_INSTANCE_TYPE, FAKE_TF_EXTRA_RESOURCE, state list)
  fake-aws.py           가짜 aws CLI (FAKE_AWS_LOG 에 호출 기록, ECR·DynamoDB·S3·태그 조회 응답 흉내)
  push-sample-image.sh  샘플 이미지 linux/amd64 빌드 → ECR 푸시 (--provenance=false 필수, Lambda 가 이미지 인덱스를 거부)
tests/
  test_worker.py     unittest 48개. `python -m unittest -v` (비용 없음, 40초 안팎)
```

- S3 backend 는 `use_lockfile=true`로 잠금 → Terraform **1.10 이상**. key 는 `deployments/<project_id>/<deploy_id>/<cloud>.tfstate` (GCP state 도 S3)
- GCP 인증은 `GOOGLE_APPLICATION_CREDENTIALS`(서비스 계정 **키 JSON 파일** 경로. 이메일 아님). 필요한 역할·API 는 README "GCP 준비"
- `.venv/` 는 AI 생성 시험 때 만든 가상환경(anthropic SDK). 지금은 쓰지 않으므로 지워도 됨 (git 제외)

### 실행 방법

```bash
python -m tfworker deploy examples/job-ec2.json
python -m tfworker status dep-demo-ec2
python -m tfworker destroy dep-demo-ec2          # destroy dep-demo-multi gcp 처럼 클라우드 하나만도 가능
python -m tfworker sweep --dry-run
python -m tfworker orphans
```

### ⚠️ 검증 상태

- 로컬 환경: Windows는 Terraform 1.16, AWS CLI v2, Python 3.14 (`python` 대신 `py`). Mac은 Terraform 1.16.4, Python `/opt/homebrew/bin/python3.12` (시스템 python3 3.9는 `str | None` 문법 때문에 안 됨)
- 확인된 것 (2026-10-02)
  - `tests/` 27개 통과: 단일·멀티 클라우드, GCP 단독, 한쪽 실패 후 그쪽만 재시도·만료 시각 유지, 단계별 실패 보고(apply·init·plan·health_check)·정리, AgentCore 모듈 사용·IaC 거부·정책 거부, 입력 오류 11종, 클라우드 하나만 destroy, state 없는 destroy 거부, sweep·orphans, digest 고정·DynamoDB 기록
  - 실제 terraform `validate`·`fmt` 통과: EC2·Lambda·Cloud Run (AWS provider 6.67, Google provider 6.50, deploy_id 40자)
  - 실제 terraform `plan` 으로 새 AWS 정책 검사(IAM 정책 허용 목록·크레딧) 통과 확인
  - **실제 AWS EC2 한 바퀴 성공** (`dep-demo-ec2b`, 249초) — `targets` 구조로 바꾸기 전 코드
  - **바뀐 구조(targets)로 EC2 한 바퀴 재확인 성공** (`dep-demo-ec2c`): ECR 태그 → digest 고정 → IaC 검사 → plan 정책 검사(5개) → apply → 헬스체크 200(121초) → 접속 확인 → destroy(5개), 남은 EC2·IAM 역할 없음
  - **실제 GCP Cloud Run 한 바퀴 성공** (`dep-demo-gcp`, 프로젝트 `softbankhackathon2026-peony`, 서울 리전 저장소 `pawploy`): 실제 plan JSON 으로 정책 검사 필드(`terraform_labels`·`template[].scaling`·`deletion_protection`) 확인 → 첫 apply 403 → 실패 보고·정리(`current_state: []`) → 권한 추가 후 같은 deploy_id 재시도 → `running`(헬스체크 200) → 앱 전용 계정 `pp-dep-demo-gcp` 로 실행 확인 → destroy 후 남은 리소스 없음
- **SQS + Fargate 워커 가동 (2026-10-02, 이미지 6b2389e)**: 서비스 실행, maintenance(만료 큐·DynamoDB scan) 동작, S3 작업 JSON → 큐 메시지 → 입력 오류 거부 → DynamoDB `status=rejected` → 메시지 삭제·작업 보호 켜고 끄기 확인 (`dep-queue-test`)
- **Fargate 워커로 실제 배포 + 1시간 자동 삭제 (2026-10-02~03)**: `dep-fargate-ec2`(Excalidraw EC2) 큐 메시지 → running(273초) → 만료 시각 Scheduler → 만료 큐 → 워커 destroy(약 50초 뒤 시작, 2분 만에 destroyed), 남은 리소스·예약 없음
- **실제 파이프라인 산출물로 AWS+GCP 동시 배포 성공 (`dep-test-1`, prj_test)**: AgentCore 분석값 + CodeBuild(pawploy-build) 이미지(ECR `pawploy-apps@sha256:757c…`, AR `pawploy/pawploy-apps@sha256:c0d7…`) + AgentCore `attempt-1/{aws,gcp}` → Fargate 워커가 최신 attempt 자동 선택 → **Lambda running(116초)**, **Cloud Run running(68초)**, 두 주소 모두 HTTP 200 (Lambda 첫 실제 배포)
- **ec2_compose (2026-10-03)**: 가짜 terraform·aws 로 배포·삭제, 이미지마다 digest 고정, 모듈로 아키텍처 판단·이미지 입력 모양 불일치 보고, 입력 오류 9종, IaC(템플릿 파일 읽기·compose 호스트 권한·입력에 없는 이미지 id)·정책(random_password 는 ec2_compose 만) 확인. 실제 terraform `validate`·`fmt` 통과(루트+모듈, random provider), 예시 템플릿 렌더 결과 `docker compose config` 통과. 컨테이너 1개 아키텍처의 렌더 결과(main.tf·tfvars)는 이전과 바이트 단위로 같음
- **아직 확인 안 된 것**: 실패 → AgentCore fix_terraform → attempt-2 재시도 한 바퀴, 3d612ae(아키텍처 자동 판단) Fargate 반영, 실제 AWS ec2_compose 한 바퀴(plan·apply)
- 해결된 의심 지점
  - `modules/lambda`: 인증 없는 함수 URL은 `lambda:InvokeFunctionUrl` + `lambda:InvokeFunction`(`invoked_via_function_url`) 둘 다 필요
  - `modules/ec2` 기본 VPC: 인터넷 게이트웨이가 지워져 경로가 `blackhole`이었음 → `default-vpc-igw` 연결로 해결. EC2 헬스체크가 `URLError`만 반복하면 이것부터 확인
  - IAM `name_prefix` 는 `substr(var.name, 0, 37)` (38 이면 deploy_id 30자 이상에서 plan 실패)
  - Cloud Run `deletion_protection` 기본값이 true 라 그대로 두면 destroy 가 실패함 → 모듈·검사에서 false 강제
  - Cloud Run 에 `PORT` 환경변수를 직접 넣으면 거부됨 (Cloud Run 이 container_port 로 자동 설정)
  - Cloud Run 생성 시 **워커 계정에 이미지 저장소 읽기 권한**(`roles/artifactregistry.reader`, 저장소 단위)이 필요. 없으면 apply 403 (`artifactregistry.repositories.downloadArtifacts`)
  - GCP 인증 키: `C:\keys\pawploy-worker.json` (저장소 밖). 사용자 환경변수에는 아직 등록 안 됨 → 실행 시 `GOOGLE_APPLICATION_CREDENTIALS` 지정 필요

---

## 4. 다음 할 일 (순서대로)

1. [x] terraform·AWS CLI 설치·로그인, 샘플 이미지 ECR 푸시, EC2 한 바퀴 (2026-10-02)
2. [x] 바뀐 구조(targets)로 EC2 한 바퀴 재확인 (2026-10-02)
3. [x] GCP 서비스 계정 키 준비 → 샘플 이미지 Artifact Registry 푸시 → Cloud Run 한 바퀴 (2026-10-02)
4. [x] AWS + GCP 동시 한 바퀴, Lambda 한 바퀴 (2026-10-03 `dep-test-1`)
5. [ ] AgentCore 담당과 모듈 약속(README "AgentCore 가 만들 Terraform 모듈") 확정, S3 경로 규칙 정하기
6. [ ] Main Server와 연결 방식 결정 (SQS / CodeBuild / ECS 작업). **Lambda에서 실행은 비추천** (15분 제한). 사용자가 Main 담당과 논의 중
   - [x] 연결 방식과 무관한 준비 (2026-10-02): 입력 오류 기록, DynamoDB 잠금·결과 이어받기, 만료 큐 소비자(`drain-destroy-queue`), 워커 컨테이너 이미지(컨테이너 안에서 GCP plan 확인)
   - [ ] 정해지면: 입구(입력을 메시지/S3 경로로 받기), 결과 알림, 워커 실행 역할(최소 권한), GCP 키를 Secrets Manager 로
7. [x] (2026-10-02 setup_fargate.py --apply) 팀 계정에 S3·상태 테이블·destroy 큐·Scheduler 역할 만들기 (`infra/setup-aws.sh --apply`, 사용자 확인 후) + `sweep`·`drain-destroy-queue` 를 5~10분 주기로 돌릴 자리
8. [ ] GCP label 기반 남은 리소스 감시 (`orphans` 의 GCP 판), Cloud Run 앱 로그 수집
9. [ ] 실제 AWS `ec2_compose` 한 바퀴 (`examples/job-ec2-compose.json`, size medium). 부팅 시간이 420초를 넘으면 HEALTH_TIMEOUT 조정
10. [ ] ECS Fargate 모듈 (공용 ALB + 배포별 대상 그룹·리스너 규칙). 추가 시 `job.CLOUD_ARCHITECTURES`·`policy.ALLOWED_TYPES` 에도 등록

---

## 5. 우리 계정에 배포하므로 지켜야 할 것

- **격리**: 사용자 앱의 권한은 로그 쓰기·이미지 읽기만. AWS 는 관리형 정책 2개만 허용, GCP 는 역할 없는 앱 전용 서비스 계정(기본 Compute 계정은 편집자 권한이라 금지)
- **워커 권한**: 지금은 관리자 권한으로 시험 중. 서버로 옮길 때 `pawploy:managed` 태그·`pawploy-` 접두사로 제한, IAM 역할 생성 시 권한 경계. 워커 권한이 넓으므로 AgentCore 코드가 워커 파일·비밀값·토큰을 읽지 못하게 `iac.py` 로 막는다
- **악용 방지**: 크기 제한, CPU 크레딧 standard, Lambda 짧은 타임아웃, Cloud Run 인스턴스 최대 1, ECS `desired_count = 1`, 동시 배포 수·시간당 배포 횟수 제한(Main Server), GPU·SageMaker 금지, AWS Budgets·GCP 예산 알림
- **삭제가 배포보다 중요**: destroy가 확실히 되는 것이 비용·악용 관리의 핵심

---

## 6. 팀원과 맞춰야 할 약속

- **AgentCore 담당**: 클라우드별 Terraform 모듈 형식(입력 6개·출력 3개, provider·backend 금지, 금지 문법 목록), S3 저장 경로, 아키텍처 이름 `ec2`/`ec2_compose`/`lambda`/`cloud_run`(/`ecs_fargate`), `ec2_compose` 의 `compose.yaml.tftpl` 템플릿 약속(README)
- **Build Worker 담당**: ECR + (GCP 선택 시) Artifact Registry 푸시, digest(`@sha256:`) 전달, `linux/amd64`, **Lambda용 이미지에 Lambda Web Adapter 포함**, 앱은 `PORT` 환경변수로 포트를 받기
- **Main Server 담당**: 21단계 입력 형식(`targets`), 호출 방식, 결과(`result.json` / DynamoDB) 읽는 법, 22단계 보고를 AgentCore 에 넘기는 방식, 재시도 시 실패한 클라우드만 보내기, 사용자 종료 요청

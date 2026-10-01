# CLAUDE.md: Pawploy Terraform Worker

claude.ai에서 나눈 설계 대화를 정리한 컨텍스트입니다. 작업 전에 끝까지 읽어 주세요.
(이전 문서에 있던 MBTI 추천, 3×3 매트릭스, 멀티 클라우드, 사용자 계정 배포 내용은 **모두 폐기**되었습니다.)

---

## 0. 사용자와 일하는 방식

- **모든 답변은 한국어로.**
- 사용자는 Terraform 초보입니다. 새 개념은 짧게 풀어서 설명하고, 명령 실행 전에 무엇을 하는지 한 줄로 말해 주세요.
- 개발 환경은 **Windows + VS Code**입니다. 셸 명령은 PowerShell 또는 Git Bash 기준으로 안내하세요.
- `terraform apply`/`destroy`, AWS 리소스 생성처럼 **비용이 생기는 명령은 실행 전에 반드시 확인**받으세요. `init`, `validate`, `plan`, `fmt`는 확인 없이 실행해도 됩니다.
- AWS 키, `.env`, state 파일은 커밋하지 않습니다.
- **현재 최우선 목표: "일단 실제로 배포가 되는 것".** 기능을 늘리기보다 EC2 한 바퀴(배포 → 접속 → 삭제)를 먼저 성공시키세요.

---

## 1. 서비스 개요 (Pawploy)

- **해커톤 주제**: One Action, Infinite Clouds: 로컬 웹앱을 원터치로 클라우드에 배포
- **대상**: 인프라를 잘 모르는 초보 개발자(바이브 코더)
- **목적**: 실제 클라우드에서 정상 배포되는지 확인하는 **테스트 환경 제공**
- **클라우드**: AWS만 사용

### 아키텍처 (확정)

```
사용자(브라우저) → Frontend(React, GitHub Pages) ↔ Main Server(EC2, Fawploy API, 전체 흐름 조율)
                                                    ├─ DynamoDB (메타데이터)
                                                    └─ 업로드 버킷 (S3, presigned URL로 직접 업로드)

⑤ 분석·추천   Main → Bedrock AgentCore: 에이전트가 S3 원본을 읽고 Claude로 배포 방식을 골라 결과 JSON을 S3에 저장
⑥ 빌드        Main → Build Worker: S3 원본으로 Docker 이미지 빌드 → ECR 푸시
⑦ 배포        Main → Terraform Worker: ECR 이미지 + 추천 결과로 Terraform 생성 → apply   ← 내 담당
⑧ 결과 안내   엔드포인트와 배포 결과를 화면에 표시
```

### 확정된 결정

- **배포는 우리(팀) AWS 계정에 한다.** 사용자 계정 배포 아님 → AssumeRole, 교차 계정 ECR 권한 필요 없음
- **악용 방지를 위해 1시간 타임아웃** 후 자동 삭제. 사용자가 직접 종료도 가능
- 배포 대상 아키텍처: Lambda, EC2, ECS Fargate (SageMaker Endpoint는 일반 웹앱 이미지로 동작하지 않고 비싸서 **이번 범위에서 제외 권장**)

---

## 2. 내 담당: Terraform Worker

> **입력**: Build Worker가 ECR에 푸시한 **이미지 주소** + AgentCore의 **추천 결과**
> **출력**: 우리 계정에 배포된 앱의 **접속 주소**와 배포 상태

### 설계 원칙

- **Terraform을 AI가 매번 생성하지 않는다.** 아키텍처별로 검증된 **모듈을 미리 만들어 두고**, 추천 결과에 맞는 모듈을 골라 **변수 파일만 생성**한다. 결과가 항상 같고 미리 테스트할 수 있음
- **모든 모듈은 같은 입력, 같은 출력**을 가진다
  - 입력: `name`, `image_uri`, `container_port`, `size`, `env`, `health_path`
  - 출력: `endpoint`, `health_url`, `resource_id`
- **배포마다 작업 폴더(`work/<deploy_id>/`)를 완결되게 만든다.** 모듈 코드까지 복사해 두므로 1시간 뒤 destroy 때 같은 코드로 정확히 지울 수 있음
- 모든 리소스에 태그: `pawploy:managed=true`, `pawploy:project_id`, `pawploy:deploy_id`, `pawploy:expires_at`

### 작업 입력 형식 (Main Server → Worker)

```json
{
  "deploy_id": "dep-demo-ec2",
  "project_id": "prj_demo",
  "architecture": "ec2",
  "image_uri": "<12자리계정>.dkr.ecr.ap-northeast-2.amazonaws.com/<저장소>:<태그 또는 @sha256:...>",
  "container_port": 8080,
  "size": "small",
  "health_path": "/",
  "env": { "APP_MODE": "test" }
}
```

- `architecture`: 현재 `ec2`, `lambda` 지원
- `size`: `micro`, `small`, `medium`만 허용 (악용 방지)
- `region`은 생략하면 이미지 주소에서 추출. Lambda는 이미지가 **같은 리전의 ECR**에 있어야 함
- `ttl_minutes`는 최대 60분으로 고정

### 상태 흐름 (`work/<deploy_id>/result.json`의 `status`)

`preparing → generating → init → plan → apply → health_check → running`
실패: `failed` (진단 후 자동 destroy 시도) / 응답 없음: `unhealthy` (진단)
삭제: `destroying → destroyed` / 삭제 실패: `destroy_failed`

---

## 3. 현재 코드 상태

**골조 (확정)**: AgentCore는 앞(분석·추천)과 뒤(실패 진단)에서만 쓴다. 아래 ①~⑤는 AI 없이 정해진 코드로 실행한다.
자세한 입력·출력 형식은 README.md 참고.

```
tfworker/
  __main__.py        진입점: deploy / destroy / status
  recommendation.py  ② AgentCore 추천 결과(recommendation_uri, S3 또는 로컬) 읽어 작업 입력에 합침
  job.py             입력 검증 (허용 아키텍처·크기, ECR 주소 형식, 환경변수 이름, TTL 최대 60분)
  image.py           ① ECR에 이미지가 있는지 확인, 태그 → @sha256 digest 고정
  render.py          ③ work/<deploy_id>/ 생성: main.tf, terraform.tfvars.json, job.json, 모듈 복사
  artifacts.py       ④ 작업 폴더를 S3(PAWPLOY_ARTIFACT_BUCKET)에 보관, destroy 때 복원
  terraform.py       ⑤ terraform CLI 실행 (로그 실시간 출력)
  health.py          헬스체크 대기 (EC2 최대 420초, Lambda 최대 180초)
  diagnose.py        failed/unhealthy 시 로그 수집 → AgentCore 진단 에이전트 호출 (PAWPLOY_DIAGNOSE_AGENT_ARN)
  awscli.py          aws CLI 실행 도우미 (PAWPLOY_OFFLINE=1이면 AWS 호출 단계 건너뜀)
modules/
  ec2/            Amazon Linux 2023 + Docker. 기본 VPC, 80번 포트만 개방(SSH 닫음), ECR 읽기 권한,
                  IMDSv2, 디스크 암호화, CPU 크레딧 standard(추가 과금 방지)
    user_data.sh.tftpl   부팅 시 Docker 설치 → ECR 로그인 → 이미지 실행 (-p 80:<container_port>)
  lambda/         이미지 패키지 Lambda + 인증 없는 함수 URL, 로그 쓰기 권한만
examples/
  job-ec2.json, job-lambda.json   작업 입력 예시 (image_uri는 실제 값으로 바꿔야 함)
  sample-app/                     테스트용 이미지 (Python 웹앱 + Lambda Web Adapter 1.1.0, EC2·Lambda 겸용)
tools/
  fake-terraform.py  가짜 terraform (FAKE_TF_FAIL, FAKE_TF_ENDPOINT 로 실패·헬스체크 대상 조절)
tests/
  test_worker.py     unittest 9개. `python -m unittest -v` (비용 없음, 10초 내외)
```

- 같은 `deploy_id`로 살아 있는 배포가 있으면 deploy를 거부한다(`render.check_not_active`, 종료 코드 2). `destroyed`/`failed`면 재사용 가능
- 헬스체크 시간은 `PAWPLOY_HEALTH_TIMEOUT` / `PAWPLOY_HEALTH_INTERVAL`로 줄일 수 있다(테스트용)
- S3 backend는 `use_lockfile=true`로 잠금 → Terraform **1.10 이상** 필요 (루트 main.tf의 required_version)

### 실행 방법

```bash
python -m tfworker deploy examples/job-ec2.json
python -m tfworker status dep-demo-ec2
python -m tfworker destroy dep-demo-ec2
```

- state는 기본적으로 작업 폴더에 로컬 저장. 환경변수 `PAWPLOY_STATE_BUCKET`을 지정하면 S3 backend 사용 (`deployments/<project_id>/<deploy_id>.tfstate`)
- `PAWPLOY_WORK_DIR`로 작업 폴더 위치 변경 가능, `TERRAFORM_BIN`으로 terraform 경로 지정 가능
- Python 표준 라이브러리만 사용 (추가 설치 없음)

### ⚠️ 검증 상태

- 로컬 환경: Windows는 Terraform 1.16, AWS CLI v2, Python 3.14 (`python` 대신 `py`). Mac은 Terraform 1.16.4(Homebrew hashicorp/tap), Python은 `/opt/homebrew/bin/python3.11` (시스템 python3 3.9는 `str | None` 문법 때문에 안 됨), aws CLI·Docker 미설치
- 확인된 것 (2026-10-02): `tests/` 9개 통과 (정상 / apply 실패 → 자동 정리 / unhealthy / 입력 오류 5종 / 재배포 거부 / state 없는 destroy 거부 / 추천 병합). 실제 terraform `validate` 통과 (EC2·Lambda, Terraform 1.16.4 + AWS provider 6.67.0)
- **아직 확인 안 된 것**: 실제 AWS 배포, ECR digest 고정, S3 보관·복원·잠금, 진단 로그 수집, AgentCore 진단 호출
- 해결된 의심 지점 (2026-10-02, 공식 문서 확인)
  - `modules/lambda`: 2025-10부터 인증 없는 함수 URL은 `lambda:InvokeFunctionUrl` + `lambda:InvokeFunction`(`invoked_via_function_url`) 둘 다 필요 → 두 번째 권한 추가함
  - `sample-app/Dockerfile`: Web Adapter `0.8.4`는 존재하지만 구버전 → 공식 README 권장 `1.1.0`으로 올림
- 아직 남은 의심 지점
  - `modules/ec2`: 팀 계정 서울 리전에 기본 VPC가 있는지 (`aws ec2 describe-vpcs --filters Name=isDefault,Values=true`)
  - EC2 콘솔 출력은 부팅 몇 분 뒤에야 채워지므로 진단 로그가 비어 있을 수 있음

---

## 4. 다음 할 일 (순서대로)

1. [ ] terraform(1.10 이상, AWS provider 6.28 이상 자동 설치)과 AWS CLI 설치·로그인 확인 (`terraform -version`, `aws sts get-caller-identity`)
2. [ ] 샘플 이미지를 **linux/amd64**로 빌드해서 ECR에 푸시 (팀 계정, 서울 리전)
3. [ ] `examples/job-ec2.json`의 `image_uri`를 실제 값으로 바꾸고, 작업 폴더에서 `terraform validate`·`plan`으로 오류 수정
4. [ ] **사용자 확인 후** EC2 실제 배포 → 접속 확인 → destroy (한 바퀴 성공이 최우선)
5. [ ] Lambda도 같은 방식으로 한 바퀴
6. [ ] apply 실패 경로와 입력 오류 경로 테스트
7. [ ] **1시간 자동 삭제**: 배포 성공 시 EventBridge Scheduler로 `expires_at`에 destroy 요청 예약 (`ActionAfterCompletion=DELETE`) + 5~10분마다 만료된 배포를 찾는 정기 점검 + `pawploy:expires_at` 태그로 남은 리소스 찾기(처음엔 알림만)
8. [ ] 상태를 `result.json` 대신 **DynamoDB**에 기록 (Main Server가 읽도록)
9. [ ] Main Server와 연결 방식 결정 (SQS로 받을지, CodeBuild/ECS 작업으로 실행할지). **Lambda에서 실행은 비추천** (15분 제한)
10. [ ] ECS Fargate 모듈 추가 (공용 ALB를 미리 만들어 두고 배포마다 대상 그룹 + 리스너 규칙만 추가하는 방식 권장)
11. [ ] plan 결과(`terraform show -json`) 정책 검사: 허용 리소스 종류, 인스턴스 타입, 필수 태그

---

## 5. 우리 계정에 배포하므로 지켜야 할 것

- **격리**: 사용자 앱의 IAM 역할에는 로그 쓰기와 ECR 읽기만. Pawploy 업로드 버킷·DynamoDB 접근 권한은 절대 주지 않기
- **워커 권한 제한**: 워커 IAM 역할은 `pawploy:managed` 태그가 붙은 리소스와 `pawploy-` 이름 접두사로 제한, IAM 역할 생성 시 권한 경계 강제
- **악용 방지**: 크기 제한, CPU 크레딧 standard, Lambda 짧은 타임아웃, ECS `desired_count = 1`, 동시 배포 수·시간당 배포 횟수 제한(Main Server), GPU·SageMaker 금지, AWS Budgets 알림
- **삭제가 배포보다 중요**: destroy가 확실히 되는 것이 비용·악용 관리의 핵심

---

## 6. 팀원과 맞춰야 할 약속

- **AgentCore 담당**: 추천 결과 JSON에서 읽을 필드(아키텍처 이름 `ec2`/`lambda`/`ecs_fargate`, 포트, 헬스체크 경로, 크기, 환경변수 이름)
- **Build Worker 담당**: 이미지 주소 형식(digest `@sha256:` 권장), `linux/amd64`, **Lambda용 이미지에 Lambda Web Adapter 포함**, ECR 리전
- **Main Server 담당**: 작업 입력 형식(위 2절), 호출 방식, 상태 기록 위치, 사용자 종료 요청 방법, 사용자 비밀값 전달 방식

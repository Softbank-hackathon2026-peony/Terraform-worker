"""작업 입력 → 배포별 작업 폴더 생성.

  work/<deploy_id>/
    result.json            클라우드별 상태를 모은 결과 (Main Server 에 돌려줄 내용)
    aws/  gcp/             target(클라우드)마다 독립된 Terraform 작업 폴더·state
      main.tf              워커가 만드는 루트: provider·필수 태그(label)·backend·모듈 호출
      modules/app/         AgentCore 가 만든 모듈(terraform_uri) 또는 저장소 기본 모듈(modules/<architecture>)
      terraform.tfvars.json, job.json

작업 폴더는 그 자체로 완결되게 만든다(모듈 복사 포함). 그래야 1시간 뒤 destroy 때
코드가 바뀌어 있어도 같은 코드로 정확히 지울 수 있다.
provider·backend·태그를 워커가 정하므로, AgentCore 가 만든 모듈이 이를 바꿀 수 없다 (iac.py 가 검사).
"""
import json
import os
import re
import shutil
from pathlib import Path

from . import awscli
from .job import (CLOUD_ARCHITECTURES, DEFAULT_ARCHITECTURE, DEFAULT_MULTI_IMAGE_ARCHITECTURE,
                  MULTI_IMAGE_ARCHITECTURES)

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "modules"
WORK = Path(os.environ.get("PAWPLOY_WORK_DIR", ROOT / "work"))

OUTPUTS = """
output "endpoint" { value = module.app.endpoint }
output "health_url" { value = module.app.health_url }
output "resource_id" { value = module.app.resource_id }
"""

# 모듈 호출. 모듈 입력은 아키텍처에 따라 두 가지다 (iac.CONTRACT_VARIABLES / COMPOSE_CONTRACT_VARIABLES 와 같아야 함)
MODULE_CALL = """
variable "project_id" { type = string }
variable "deploy_id" { type = string }
variable "expires_at" { type = string }
variable "image_uri" { type = string }
variable "container_port" { type = number }
variable "size" { type = string }
variable "health_path" { type = string }
variable "env" { type = map(string) }

module "app" {
  source         = "./modules/app"
  name           = "pawploy-${var.deploy_id}"
  image_uri      = var.image_uri
  container_port = var.container_port
  size           = var.size
  env            = var.env
  health_path    = var.health_path
}
""" + OUTPUTS

# 여러 컨테이너(ec2_compose): image_uri 하나 대신 images(이미지 id → digest 고정 ECR 주소).
# 포트·환경변수는 AgentCore 가 렌더한 모듈 안의 compose.yaml.tftpl 에 들어 있으므로 넘기지 않는다
COMPOSE_MODULE_CALL = """
variable "project_id" { type = string }
variable "deploy_id" { type = string }
variable "expires_at" { type = string }
variable "images" { type = map(string) }
variable "size" { type = string }
variable "health_path" { type = string }

module "app" {
  source      = "./modules/app"
  name        = "pawploy-${var.deploy_id}"
  images      = var.images
  size        = var.size
  health_path = var.health_path
}
""" + OUTPUTS

# ec2_compose 모듈의 random_password(데이터 저장소 비밀번호)용. 모듈은 provider 를 정할 수 없으므로 루트에서 고정
RANDOM_PROVIDER = """    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
"""

AWS_MAIN_TF = """\
terraform {{
  required_version = ">= 1.10" # S3 backend 의 use_lockfile 이 1.10 부터 지원됨
{backend}
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 6.28" # aws_lambda_permission.invoked_via_function_url 이 6.28.0 부터
    }}
{extra_providers}  }}
}}

provider "aws" {{
  region = var.region
  default_tags {{
    tags = {{
      "pawploy:managed"    = "true"
      "pawploy:project_id" = var.project_id
      "pawploy:deploy_id"  = var.deploy_id
      "pawploy:expires_at" = var.expires_at
    }}
  }}
}}

variable "region" {{ type = string }}
{module_call}{scheduler}"""

GCP_MAIN_TF = """\
terraform {{
  required_version = ">= 1.10"
{backend}
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 6.0"
    }}
  }}
}}

# 인증: GOOGLE_APPLICATION_CREDENTIALS (서비스 계정 키 파일) 등 Application Default Credentials
provider "google" {{
  project = var.gcp_project
  region  = var.region
  # GCP label 은 키·값에 ":"·대문자를 쓸 수 없어 AWS 태그와 이름을 맞춰 바꾼다 (policy.REQUIRED_LABELS)
  default_labels = var.labels
}}

variable "gcp_project" {{ type = string }}
variable "region" {{ type = string }}
variable "labels" {{ type = map(string) }}
{module_call}"""

# 만료 시각에 destroy 요청을 보내는 예약 (expire.py 의 1번 그물). AWS target 이고 환경변수가 둘 다 있을 때만.
# 큐와 역할은 팀 계정에 한 번만 만들어 둔다 (역할: scheduler.amazonaws.com 신뢰 + 그 큐에 sqs:SendMessage).
SCHEDULER_TF = """
# ---------------- 만료 시각 destroy 예약 ----------------
# 한 번 실행된 뒤 스케줄은 스스로 지워진다(DELETE). 그 전에 destroy 되면 state 와 함께 지워진다.
variable "destroy_queue_arn" { type = string }
variable "scheduler_role_arn" { type = string }

resource "aws_scheduler_schedule" "expire" {
  name                         = "pawploy-${var.deploy_id}-expire"
  schedule_expression          = "at(${trimsuffix(var.expires_at, "Z")})"
  schedule_expression_timezone = "UTC"
  action_after_completion      = "DELETE"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = var.destroy_queue_arn
    role_arn = var.scheduler_role_arn
    input    = jsonencode({ action = "destroy", deploy_id = var.deploy_id, project_id = var.project_id })
  }
}
"""

TFVAR_KEYS = ("region", "project_id", "deploy_id", "expires_at", "image_uri",
              "container_port", "size", "health_path", "env")
COMPOSE_TFVAR_KEYS = ("region", "project_id", "deploy_id", "expires_at", "images", "size", "health_path")


def module_call(architecture: str) -> str:
    """루트 main.tf 의 변수 선언 + module "app" 호출 + 출력."""
    return COMPOSE_MODULE_CALL if architecture in MULTI_IMAGE_ARCHITECTURES else MODULE_CALL


def workdir_for(deploy_id: str) -> Path:
    return WORK / deploy_id


def target_dir(deploy_id: str, cloud: str) -> Path:
    return workdir_for(deploy_id) / cloud


def target_job(job: dict, target: dict) -> dict:
    """공통 입력 + target 하나를 합친 클라우드별 작업. 이후 단계는 모두 이 단위로 돈다."""
    shared = {k: v for k, v in job.items() if k != "targets"}
    return {**shared, **target}


def gcp_label(value: str) -> str:
    """GCP label 값: 소문자·숫자·_·- 만, 최대 63자."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())[:63]


def render(tjob: dict) -> Path:
    wd = target_dir(tjob["deploy_id"], tjob["cloud"])
    wd.mkdir(parents=True, exist_ok=True)

    # 모듈: AgentCore 가 만든 것(terraform_uri) 또는 기본 모듈. 배포 시점의 코드를 작업 폴더에 고정한다
    dst = wd / "modules" / "app"
    shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fetch_module(tjob, dst)

    # state 저장 위치: 환경변수 PAWPLOY_STATE_BUCKET이 있으면 S3, 없으면 작업 폴더(로컬)
    # job.json에 기록해 두어 destroy 때 환경변수가 달라도 같은 state를 찾게 한다
    tjob["state_bucket"] = os.environ.get("PAWPLOY_STATE_BUCKET") or None
    tjob["state_region"] = os.environ.get("PAWPLOY_STATE_REGION") or (
        tjob["region"] if tjob["cloud"] == "aws" else os.environ.get("PAWPLOY_REGION", "ap-northeast-2"))
    backend = '  backend "s3" {}\n' if tjob["state_bucket"] else ""

    multi_image = tjob["architecture"] in MULTI_IMAGE_ARCHITECTURES
    tfvars = {k: tjob[k] for k in (COMPOSE_TFVAR_KEYS if multi_image else TFVAR_KEYS)}
    if tjob["cloud"] == "aws":
        # 만료 시각 destroy 예약: 큐·역할 ARN 이 둘 다 있을 때만 (없으면 sweep 정기 점검만으로 지운다)
        tjob["destroy_queue_arn"] = os.environ.get("PAWPLOY_DESTROY_QUEUE_ARN") or None
        tjob["scheduler_role_arn"] = os.environ.get("PAWPLOY_SCHEDULER_ROLE_ARN") or None
        scheduled = bool(tjob["destroy_queue_arn"] and tjob["scheduler_role_arn"])
        main_tf = AWS_MAIN_TF.format(backend=backend, scheduler=SCHEDULER_TF if scheduled else "",
                                     module_call=module_call(tjob["architecture"]),
                                     extra_providers=RANDOM_PROVIDER if multi_image else "")
        if scheduled:
            tfvars.update(destroy_queue_arn=tjob["destroy_queue_arn"], scheduler_role_arn=tjob["scheduler_role_arn"])
    else:
        main_tf = GCP_MAIN_TF.format(backend=backend, module_call=module_call(tjob["architecture"]))
        tfvars.update(gcp_project=tjob["gcp_project"], labels={
            "pawploy-managed": "true",
            "pawploy-project-id": gcp_label(tjob["project_id"]),
            "pawploy-deploy-id": gcp_label(tjob["deploy_id"]),
            "pawploy-expires-at": gcp_label(tjob["expires_at"]),
        })

    (wd / "main.tf").write_text(main_tf, encoding="utf-8")
    (wd / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2, ensure_ascii=False), encoding="utf-8")
    (wd / "job.json").write_text(json.dumps(tjob, indent=2, ensure_ascii=False), encoding="utf-8")
    return wd


class ModuleError(Exception):
    """AgentCore 모듈을 쓸 수 없음 (파일 없음·아키텍처 불일치 등) → generating 단계 실패로 보고."""


ATTEMPT_RE = re.compile(r"attempt-(\d+)/$")


def _s3_subdirs(uri: str) -> list[str]:
    """s3://버킷/경로/ 바로 아래 폴더 이름들 (끝에 / 포함)."""
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    out = awscli.run("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--delimiter", "/")
    return [c["Prefix"][len(prefix):] for c in (out or {}).get("CommonPrefixes") or []]


def module_uri(tjob: dict) -> str | None:
    """AgentCore 모듈 위치를 정한다. None 이면 저장소 기본 모듈.

    AgentCore 저장 규칙: s3://<PAWPLOY_AGENT_BUCKET>/projects/<project_id>/deploy/<deploy_id>/attempt-<N>/
      - terraform_uri 가 없고 PAWPLOY_AGENT_BUCKET 이 있으면 위 규칙의 deploy 폴더에서 찾는다
      - terraform_uri 가 attempt-N 상위 폴더(attempt-* 를 담은 폴더)면 그 안에서 찾는다
      - N 이 가장 큰 attempt 를 쓴다 (숫자 비교: attempt-10 > attempt-9). 수정본(23~25)이 N+1 로 올라온다
      - attempt-N/<cloud>/ 폴더가 있으면 그 폴더(멀티 클라우드), 없으면 attempt-N/ 자체
      - terraform_uri 가 이미 <cloud>/ 폴더를 가리키면 S3 목록을 조회하지 않고 그대로 쓴다 (조회 1회 약 1.8초)
    """
    uri = tjob.get("terraform_uri")
    bucket = os.environ.get("PAWPLOY_AGENT_BUCKET")
    if not uri and not bucket:
        return None
    if uri and uri.startswith("s3://") and uri.endswith(f"/{tjob['cloud']}/"):
        return uri
    if not uri:
        uri = f"s3://{bucket}/projects/{tjob['project_id']}/deploy/{tjob['deploy_id']}/"
    if uri.startswith("s3://") and not ATTEMPT_RE.search(uri):
        attempts = [(int(m.group(1)), d) for d in _s3_subdirs(uri) if (m := ATTEMPT_RE.fullmatch(d))]
        if attempts:
            uri += max(attempts)[1]
        elif not tjob.get("terraform_uri"):
            print(f"[render] {tjob['cloud']}: {uri} 에 AgentCore 모듈이 없어 기본 모듈 사용", flush=True)
            return None
    if uri.startswith("s3://") and f"{tjob['cloud']}/" in _s3_subdirs(uri):
        uri += f"{tjob['cloud']}/"
    return uri


# 아키텍처를 알려 주는 대표 리소스 (모듈 하나에 하나만 있어야 한다)
ARCHITECTURE_MARKERS = {
    "ec2": "aws_instance",
    "lambda": "aws_lambda_function",
    "cloud_run": "google_cloud_run_v2_service",
}
# aws_instance 모듈 중 이 파일이 있으면 여러 컨테이너(Docker Compose) 실행기 ec2_compose
COMPOSE_TEMPLATE = "compose.yaml.tftpl"


def detect_architecture(module_dir: Path) -> str | None:
    code = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in sorted(module_dir.glob("*.tf")))
    found = {arch for arch, rtype in ARCHITECTURE_MARKERS.items()
             if re.search(rf'^\s*resource\s+"{rtype}"\s+"', code, re.M)}
    if len(found) > 1:
        raise ModuleError(f"모듈 하나에 아키텍처가 여러 개입니다: {sorted(found)}")
    arch = found.pop() if found else None
    if arch == "ec2" and (module_dir / COMPOSE_TEMPLATE).is_file():
        return "ec2_compose"
    return arch


def resolve_architecture(tjob: dict, module_dir: Path) -> None:
    """AgentCore 모듈의 아키텍처를 정한다. 입력에 있으면 모듈과 같은지 확인하고, 없으면 모듈에서 알아낸다."""
    given, detected = tjob.get("architecture"), detect_architecture(module_dir)
    if given and detected and given != detected:
        raise ModuleError(f"입력 architecture={given} 와 AgentCore 모듈({detected})이 다릅니다: {tjob['terraform_source']}")
    arch = given or detected
    if arch not in CLOUD_ARCHITECTURES[tjob["cloud"]]:
        raise ModuleError(f"{tjob['cloud']} 모듈의 아키텍처를 알 수 없습니다 "
                           f"(대표 리소스 {sorted(ARCHITECTURE_MARKERS.values())} 중 하나가 필요): {tjob['terraform_source']}")
    tjob["architecture"] = arch


def check_image_input(tjob: dict) -> None:
    """정해진 아키텍처와 이미지 입력 모양이 맞는지: ec2_compose 는 images, 나머지는 image_uri."""
    arch, multi = tjob["architecture"], bool(tjob.get("images"))
    if arch in MULTI_IMAGE_ARCHITECTURES and not multi:
        raise ModuleError(f"모듈 아키텍처 {arch} 는 images(이미지 id → ECR 주소)가 필요한데 입력은 image_uri 입니다: "
                          f"{tjob['terraform_source']}")
    if arch not in MULTI_IMAGE_ARCHITECTURES and multi:
        raise ModuleError(f"모듈 아키텍처 {arch} 는 image_uri 하나만 받는데 입력은 images 입니다: {tjob['terraform_source']}")


def fetch_module(tjob: dict, dst: Path) -> None:
    uri = module_uri(tjob)
    if not uri:
        tjob["architecture"] = tjob.get("architecture") or (
            DEFAULT_MULTI_IMAGE_ARCHITECTURE if tjob.get("images") else DEFAULT_ARCHITECTURE[tjob["cloud"]])
    tjob["terraform_source"] = uri or f"modules/{tjob['architecture']}"
    if not uri:
        shutil.copytree(MODULES / tjob["architecture"], dst)
        print(f"[render] {tjob['cloud']}: 기본 모듈 modules/{tjob['architecture']} 사용", flush=True)
    elif uri.startswith("s3://"):
        dst.mkdir(parents=True)
        awscli.run("s3", "sync", uri, str(dst), "--only-show-errors", json_output=False)
        if not any(dst.glob("*.tf")):
            raise ModuleError(f"AgentCore 모듈에 .tf 파일이 없습니다: {uri}")
        print(f"[render] {tjob['cloud']}: AgentCore 모듈 {uri}", flush=True)
    else:
        shutil.copytree(uri, dst)
        print(f"[render] {tjob['cloud']}: 로컬 모듈 {uri}", flush=True)
    if uri:
        resolve_architecture(tjob, dst)
        print(f"[render] {tjob['cloud']}: 아키텍처 {tjob['architecture']}", flush=True)
    check_image_input(tjob)


def backend_args(tjob: dict) -> list[str]:
    bucket = tjob.get("state_bucket")
    if not bucket:
        return []
    return [
        f"-backend-config=bucket={bucket}",
        f"-backend-config=key=deployments/{tjob['project_id']}/{tjob['deploy_id']}/{tjob['cloud']}.tfstate",
        f"-backend-config=region={tjob.get('state_region') or tjob['region']}",
        # S3 네이티브 잠금(<key>.tflock). 같은 배포에 deploy/destroy가 동시에 들어와도 state가 깨지지 않는다.
        # Terraform 1.10+ 전용. DynamoDB 잠금은 deprecated라 쓰지 않는다.
        "-backend-config=use_lockfile=true",
    ]

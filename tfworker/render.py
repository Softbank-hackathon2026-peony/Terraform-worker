"""작업 입력 → 배포별 작업 폴더(work/<deploy_id>) 생성.

작업 폴더는 그 자체로 완결되게 만든다(모듈 복사 포함). 그래야 1시간 뒤 destroy 때
코드가 바뀌어 있어도 같은 코드로 정확히 지울 수 있다.
"""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "modules"
WORK = Path(os.environ.get("PAWPLOY_WORK_DIR", ROOT / "work"))

MAIN_TF = """\
terraform {{
  required_version = ">= 1.10" # S3 backend 의 use_lockfile 이 1.10 부터 지원됨
{backend}
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 6.28" # aws_lambda_permission.invoked_via_function_url 이 6.28.0 부터
    }}
  }}
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
variable "project_id" {{ type = string }}
variable "deploy_id" {{ type = string }}
variable "expires_at" {{ type = string }}
variable "image_uri" {{ type = string }}
variable "container_port" {{ type = number }}
variable "size" {{ type = string }}
variable "health_path" {{ type = string }}
variable "env" {{ type = map(string) }}

module "app" {{
  source         = "./modules/{architecture}"
  name           = "pawploy-${{var.deploy_id}}"
  image_uri      = var.image_uri
  container_port = var.container_port
  size           = var.size
  env            = var.env
  health_path    = var.health_path
}}

output "endpoint" {{ value = module.app.endpoint }}
output "health_url" {{ value = module.app.health_url }}
output "resource_id" {{ value = module.app.resource_id }}
{scheduler}"""

# 만료 시각에 destroy 요청을 보내는 예약 (expire.py 의 1번 그물). 환경변수가 둘 다 있을 때만 main.tf 에 들어간다.
# 큐와 역할은 팀 계정에 한 번만 만들어 둔다 (역할: scheduler.amazonaws.com 신뢰 + 그 큐에 sqs:SendMessage).
# 요청을 받아 `python -m tfworker destroy` 를 실행하는 소비자는 Main Server 연결 방식과 함께 정한다.
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


# 이 상태들은 "리소스가 남아 있지 않다"고 판단해 같은 deploy_id 를 다시 써도 된다
REUSABLE_STATUSES = {None, "destroyed", "failed"}


def workdir_for(deploy_id: str) -> Path:
    return WORK / deploy_id


def check_not_active(deploy_id: str) -> None:
    """같은 deploy_id 로 진행 중이거나 살아 있는 배포가 있으면 거부한다.

    그냥 덮어쓰면 기존 state 위에 새 코드가 올라가 리소스가 바뀌거나(의도치 않은 재사용),
    실패 시 어느 리소스가 누구 것인지 알 수 없게 된다. Main Server 는 배포마다 새 deploy_id 를 써야 한다.
    """
    result = workdir_for(deploy_id) / "result.json"
    if not result.exists():
        return
    try:
        status = json.loads(result.read_text(encoding="utf-8")).get("status")
    except ValueError:
        status = "unknown"
    if status not in REUSABLE_STATUSES:
        raise ValueError(f"이미 사용 중인 deploy_id 입니다: {deploy_id} (status={status}). "
                         f"먼저 destroy 하거나 새 deploy_id 를 쓰세요")


def render(job: dict) -> Path:
    # check_not_active 는 진입점(__main__.deploy)이 result.json 을 쓰기 전에 호출한다
    wd = workdir_for(job["deploy_id"])
    wd.mkdir(parents=True, exist_ok=True)

    # 모듈 복사 (배포 시점의 코드를 고정)
    dst = wd / "modules" / job["architecture"]
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(MODULES / job["architecture"], dst)

    # state 저장 위치: 환경변수 PAWPLOY_STATE_BUCKET이 있으면 S3, 없으면 작업 폴더(로컬)
    # job.json에 기록해 두어 destroy 때 환경변수가 달라도 같은 state를 찾게 한다
    job["state_bucket"] = os.environ.get("PAWPLOY_STATE_BUCKET") or None
    job["state_region"] = os.environ.get("PAWPLOY_STATE_REGION", job["region"])
    backend = '  backend "s3" {}\n' if job["state_bucket"] else ""

    # 만료 시각 destroy 예약: 큐·역할 ARN 이 둘 다 있을 때만 (없으면 sweep 정기 점검만으로 지운다)
    job["destroy_queue_arn"] = os.environ.get("PAWPLOY_DESTROY_QUEUE_ARN") or None
    job["scheduler_role_arn"] = os.environ.get("PAWPLOY_SCHEDULER_ROLE_ARN") or None
    scheduled = bool(job["destroy_queue_arn"] and job["scheduler_role_arn"])

    (wd / "main.tf").write_text(
        MAIN_TF.format(backend=backend, architecture=job["architecture"],
                       scheduler=SCHEDULER_TF if scheduled else ""), encoding="utf-8")

    tfvars = {k: job[k] for k in (
        "region", "project_id", "deploy_id", "expires_at", "image_uri",
        "container_port", "size", "health_path", "env")}
    if scheduled:
        tfvars.update(destroy_queue_arn=job["destroy_queue_arn"], scheduler_role_arn=job["scheduler_role_arn"])
    (wd / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2, ensure_ascii=False), encoding="utf-8")
    (wd / "job.json").write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
    return wd


def backend_args(job: dict) -> list[str]:
    bucket = job.get("state_bucket")
    if not bucket:
        return []
    return [
        f"-backend-config=bucket={bucket}",
        f"-backend-config=key=deployments/{job['project_id']}/{job['deploy_id']}.tfstate",
        f"-backend-config=region={job.get('state_region') or job['region']}",
        # S3 네이티브 잠금(<key>.tflock). 같은 배포에 deploy/destroy가 동시에 들어와도 state가 깨지지 않는다.
        # Terraform 1.10+ 전용. DynamoDB 잠금은 deprecated라 쓰지 않는다.
        "-backend-config=use_lockfile=true",
    ]

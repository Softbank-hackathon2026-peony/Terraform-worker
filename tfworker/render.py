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
  required_version = ">= 1.5"
{backend}
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
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

variable "region"         {{ type = string }}
variable "project_id"     {{ type = string }}
variable "deploy_id"      {{ type = string }}
variable "expires_at"     {{ type = string }}
variable "image_uri"      {{ type = string }}
variable "container_port" {{ type = number }}
variable "size"           {{ type = string }}
variable "health_path"    {{ type = string }}
variable "env"            {{ type = map(string) }}

module "app" {{
  source         = "./modules/{architecture}"
  name           = "pawploy-${{var.deploy_id}}"
  image_uri      = var.image_uri
  container_port = var.container_port
  size           = var.size
  env            = var.env
  health_path    = var.health_path
}}

output "endpoint"    {{ value = module.app.endpoint }}
output "health_url"  {{ value = module.app.health_url }}
output "resource_id" {{ value = module.app.resource_id }}
"""


def workdir_for(deploy_id: str) -> Path:
    return WORK / deploy_id


def render(job: dict) -> Path:
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
    (wd / "main.tf").write_text(MAIN_TF.format(backend=backend, architecture=job["architecture"]), encoding="utf-8")

    tfvars = {k: job[k] for k in (
        "region", "project_id", "deploy_id", "expires_at", "image_uri",
        "container_port", "size", "health_path", "env")}
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
    ]

"""4단계: 생성된 Terraform 코드(작업 폴더)를 S3에 보관.

destroy를 다른 머신이나 컨테이너에서 해도 배포 당시 코드를 그대로 받아 쓰기 위해서다.
PAWPLOY_ARTIFACT_BUCKET이 없으면 건너뛰고 로컬 작업 폴더만 쓴다.

  s3://<버킷>/workdirs/<deploy_id>/<cloud>/main.tf, terraform.tfvars.json, job.json, modules/app/...
"""
import os
from pathlib import Path

from . import awscli

# state는 S3 backend가 따로 관리하고, 나머지는 실행할 때마다 다시 생기는 파일이다
EXCLUDES = [".terraform/*", "tfplan", "plan.json", "*.tfstate", "*.tfstate.*", "result.json", "diagnosis_*.json"]


def bucket() -> str | None:
    return os.environ.get("PAWPLOY_ARTIFACT_BUCKET")


def s3_prefix(deploy_id: str) -> str:
    return f"s3://{bucket()}/workdirs/{deploy_id}/"


def upload(tjob: dict, wd: Path) -> str | None:
    """클라우드 하나의 작업 폴더(work/<deploy_id>/<cloud>)를 보관한다."""
    if not bucket() or awscli.offline():
        print("[artifacts] PAWPLOY_ARTIFACT_BUCKET 없음 → S3 보관 건너뜀", flush=True)
        return None
    dest = f"{s3_prefix(tjob['deploy_id'])}{tjob['cloud']}/"
    args = ["s3", "sync", str(wd), dest, "--only-show-errors"]
    for pattern in EXCLUDES:
        args += ["--exclude", pattern]
    awscli.run(*args, json_output=False)
    print(f"[artifacts] 보관 완료: {dest}", flush=True)
    return dest


def download(deploy_id: str, wd: Path) -> bool:
    """S3에 보관한 배포 폴더(클라우드별 하위 폴더 포함)를 내려받는다. job.json 이 하나라도 있으면 True."""
    if not bucket() or awscli.offline():
        return False
    awscli.run("s3", "sync", s3_prefix(deploy_id), str(wd), "--only-show-errors", json_output=False)
    return any(wd.glob("*/job.json"))

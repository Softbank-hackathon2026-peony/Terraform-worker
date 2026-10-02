#!/usr/bin/env python3
"""가짜 terraform: AWS 리소스 없이 워커의 deploy → 헬스체크 → destroy 흐름을 시험한다.

사용법 (PAWPLOY_OFFLINE=1 과 함께):
  TERRAFORM_BIN=tools/fake-terraform.py python -m tfworker deploy examples/job-ec2.json

실제 terraform 처럼 -chdir=<작업폴더> 를 받아 그 안에 흔적을 남긴다.
  init    → .terraform.lock.hcl
  plan    → tfplan
  apply   → terraform.tfstate (리소스가 "생긴" 것으로 간주)
  destroy → terraform.tfstate 를 빈 state 로 바꿈
  output  → endpoint / health_url / resource_id JSON

환경변수
  FAKE_TF_FAIL            init | plan | apply | destroy 중 하나. 그 단계에서 exit 1 로 실패한다
  FAKE_TF_ENDPOINT        output 의 endpoint (기본 http://127.0.0.1:9 → 연결 거부 → unhealthy 경로)
  FAKE_TF_INSTANCE_TYPE   show -json 이 돌려주는 EC2 인스턴스 타입 (기본 t3.small, 정책 위반 시험용)
  FAKE_TF_EXTRA_RESOURCE  show -json 에 추가로 끼워 넣을 리소스 종류 (예: aws_s3_bucket, 정책 위반 시험용)
"""
import json
import os
import sys
from pathlib import Path

TAGS = {"pawploy:managed": "true", "pawploy:project_id": "prj", "pawploy:deploy_id": "dep", "pawploy:expires_at": "x"}


def _change(address: str, rtype: str, after: dict, tagged: bool = True) -> dict:
    if tagged:
        after = {**after, "tags_all": TAGS}
    return {"address": address, "mode": "managed", "type": rtype, "name": address.rsplit(".", 1)[-1],
            "change": {"actions": ["create"], "before": None, "after": after}}


def _fake_plan(workdir: Path) -> dict:
    """실제 모듈(modules/ec2, modules/lambda)이 만드는 리소스와 같은 종류·속성을 가진 plan JSON."""
    architecture = "ec2"
    job_file = workdir / "job.json"
    if job_file.exists():
        architecture = json.loads(job_file.read_text(encoding="utf-8")).get("architecture", "ec2")

    if architecture == "lambda":
        changes = [
            _change("module.app.aws_iam_role.app", "aws_iam_role", {}),
            _change("module.app.aws_iam_role_policy_attachment.logs", "aws_iam_role_policy_attachment", {}, tagged=False),
            _change("module.app.aws_lambda_function.app", "aws_lambda_function", {"memory_size": 1024, "timeout": 30}),
            _change("module.app.aws_lambda_function_url.app", "aws_lambda_function_url", {}, tagged=False),
            _change("module.app.aws_lambda_permission.public_url", "aws_lambda_permission", {}, tagged=False),
        ]
    else:
        changes = [
            _change("module.app.aws_security_group.app", "aws_security_group",
                    {"ingress": [{"from_port": 80, "to_port": 80, "protocol": "tcp"}]}),
            _change("module.app.aws_iam_role.app", "aws_iam_role", {}),
            _change("module.app.aws_iam_role_policy_attachment.ecr_read", "aws_iam_role_policy_attachment", {}, tagged=False),
            _change("module.app.aws_iam_instance_profile.app", "aws_iam_instance_profile", {}),
            _change("module.app.aws_instance.app", "aws_instance",
                    {"instance_type": os.environ.get("FAKE_TF_INSTANCE_TYPE", "t3.small")}),
        ]
    extra = os.environ.get("FAKE_TF_EXTRA_RESOURCE")
    if extra:
        changes.append(_change(f"{extra}.extra", extra, {}))
    return {"format_version": "1.2", "terraform_version": "9.9.9-fake", "resource_changes": changes}


def main(argv: list[str]) -> int:
    workdir = Path(".")
    args = []
    for a in argv:
        if a.startswith("-chdir="):
            workdir = Path(a.split("=", 1)[1])
        else:
            args.append(a)
    if not args:
        print("Usage: terraform [global options] <subcommand> [args]")
        return 1
    cmd = args[0]

    if cmd == "-version" or cmd == "version":
        print("Terraform v9.9.9-fake")
        return 0

    if os.environ.get("FAKE_TF_FAIL") == cmd:
        print(f"[fake-terraform] {cmd}: 의도된 실패 (FAKE_TF_FAIL={cmd})")
        print("Error: creating EC2 Instance: InvalidParameterValue (fake)")
        return 1

    state = workdir / "terraform.tfstate"
    if cmd == "init":
        (workdir / ".terraform.lock.hcl").write_text("# fake lock\n", encoding="utf-8")
        print("Terraform has been successfully initialized! (fake)")
    elif cmd == "validate":
        print("Success! The configuration is valid. (fake)")
    elif cmd == "plan":
        (workdir / "tfplan").write_bytes(b"fake-plan")
        print("Plan: 7 to add, 0 to change, 0 to destroy. (fake)")
    elif cmd == "show":
        # `show -json tfplan` → 정책 검사(policy.py)가 읽는 plan JSON. 실제 모듈이 만드는 리소스를 흉내 낸다
        print(json.dumps(_fake_plan(workdir)))
    elif cmd == "apply":
        state.write_text(json.dumps({"version": 4, "fake": True,
                                     "resources": [{"type": "aws_instance", "name": "app"}]}), encoding="utf-8")
        print("Apply complete! Resources: 7 added, 0 changed, 0 destroyed. (fake)")
    elif cmd == "destroy":
        if state.exists():
            state.write_text(json.dumps({"version": 4, "fake": True, "resources": []}), encoding="utf-8")
        print("Destroy complete! Resources: 7 destroyed. (fake)")
    elif cmd == "output":
        endpoint = os.environ.get("FAKE_TF_ENDPOINT", "http://127.0.0.1:9").rstrip("/")
        health_path = "/"
        tfvars = workdir / "terraform.tfvars.json"
        if tfvars.exists():
            health_path = json.loads(tfvars.read_text(encoding="utf-8")).get("health_path", "/")
        out = {
            "endpoint": {"value": endpoint, "type": "string"},
            "health_url": {"value": endpoint + health_path, "type": "string"},
            "resource_id": {"value": "i-0fake000000000000", "type": "string"},
        }
        print(json.dumps(out))
    else:
        print(f"[fake-terraform] 모르는 명령: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

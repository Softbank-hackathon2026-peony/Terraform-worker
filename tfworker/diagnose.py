"""배포 실패·응답 없음 진단 (AgentCore를 쓰는 뒤쪽 지점).

1) 진단 자료 모으기: terraform 로그 + 앱 로그(EC2 콘솔 출력 / Lambda CloudWatch Logs)
   → work/<deploy_id>/diagnosis_input.json
2) PAWPLOY_DIAGNOSE_AGENT_ARN이 있으면 AgentCore 진단 에이전트에 보내 쉬운 설명을 받는다.
   → work/<deploy_id>/diagnosis_output.json, result.json의 diagnosis

진단 에이전트에는 이 자료만 넘긴다(환경변수는 이름만, 값은 보내지 않음).
에이전트에게는 배포 권한이 없고, 수정 제안을 반영한 재배포도 새 작업 입력으로 job.py 검증을 다시 거친다.
진단은 부가 기능이라 여기서 나는 오류는 배포 결과를 바꾸지 않는다.
"""
import json
import os
import uuid
from pathlib import Path

from . import awscli

MAX_LOG = 8000


def run(job: dict, wd: Path, result: dict) -> dict | None:
    try:
        context = collect(job, result)
        path = wd / "diagnosis_input.json"
        path.write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[diagnose] 진단 자료 저장: {path}", flush=True)

        arn = os.environ.get("PAWPLOY_DIAGNOSE_AGENT_ARN")
        if not arn or awscli.offline():
            return None
        return invoke_agent(arn, job, wd)
    except Exception as e:  # 진단 실패가 배포·삭제 흐름을 막으면 안 됨
        print(f"[diagnose] 진단 생략: {e}", flush=True)
        return None


def collect(job: dict, result: dict) -> dict:
    context = {
        "deploy_id": job["deploy_id"],
        "architecture": job["architecture"],
        "status": result.get("status"),
        "error": result.get("error"),
        "health_url": result.get("health_url"),
        "job": {k: job[k] for k in ("container_port", "health_path", "size")},
        "env_names": sorted(job["env"]),
        "terraform_log_tail": result.get("log_tail", ""),
    }
    if result.get("resource_id") and not awscli.offline():
        context["app_log"] = _app_log(job, result["resource_id"])
    return context


def _app_log(job: dict, resource_id: str) -> str:
    try:
        if job["architecture"] == "ec2":
            # 부팅 스크립트(user_data) 출력이 콘솔에 남는다
            out = awscli.run("ec2", "get-console-output", "--instance-id", resource_id, "--latest",
                             region=job["region"])
            text = out.get("Output") or ""
        else:
            text = awscli.run("logs", "tail", f"/aws/lambda/pawploy-{job['deploy_id']}", "--since", "30m",
                              region=job["region"], json_output=False)
    except awscli.AwsError as e:
        text = f"(앱 로그를 가져오지 못함: {e})"
    return text[-MAX_LOG:]


def invoke_agent(arn: str, job: dict, wd: Path) -> dict:
    # ⚠️ 실제 AgentCore 에이전트로 아직 시험하지 않은 호출이다
    region = arn.split(":")[3]  # arn:aws:bedrock-agentcore:<리전>:<계정>:runtime/<id>
    out = wd / "diagnosis_output.json"
    session_id = f"pawploy-diag-{job['deploy_id']}-{uuid.uuid4().hex}"  # 33자 이상이어야 함
    awscli.run("bedrock-agentcore", "invoke-agent-runtime",
               "--agent-runtime-arn", arn,
               "--runtime-session-id", session_id,
               "--content-type", "application/json",
               "--payload", f"fileb://{(wd / 'diagnosis_input.json').as_posix()}",
               str(out), region=region, json_output=False)
    text = out.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return {"summary": text.strip()}

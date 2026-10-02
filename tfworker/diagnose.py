"""실패 진단 자료 수집 (파이프라인 22단계의 "오류 로그"에 함께 싣는다).

배포는 됐는데 앱이 응답하지 않을 때, 리소스를 지우기 전에 앱 로그를 모은다.
  EC2: 콘솔 출력(부팅 스크립트 로그)   Lambda: CloudWatch Logs   Cloud Run: (아직 수집 안 함, gcloud 필요)
  ec2_compose: EC2 와 같은 콘솔 출력 (부팅 스크립트가 컨테이너 상태·로그 끝부분을 함께 남긴다)
AgentCore 에 직접 보내지 않는다. Main Server 가 22단계 보고를 받아 23단계에서 AgentCore 에 넘긴다.
진단은 부가 기능이라 여기서 나는 오류는 배포·삭제 흐름을 바꾸지 않는다.
"""
from . import awscli

MAX_LOG = 8000


def app_log(tjob: dict, resource_id: str | None) -> str | None:
    if not resource_id or awscli.offline() or tjob["cloud"] != "aws":
        return None
    try:
        if tjob["architecture"] in ("ec2", "ec2_compose"):
            out = awscli.run("ec2", "get-console-output", "--instance-id", resource_id, "--latest",
                             region=tjob["region"])
            text = out.get("Output") or ""
        else:
            text = awscli.run("logs", "tail", f"/aws/lambda/pawploy-{tjob['deploy_id']}", "--since", "30m",
                              region=tjob["region"], json_output=False)
    except Exception as e:  # 진단 실패가 정리(destroy)를 막으면 안 됨
        text = f"(앱 로그를 가져오지 못함: {e})"
    return text[-MAX_LOG:]

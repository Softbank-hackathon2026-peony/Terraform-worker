"""상태 기록: result.json(로컬) 과 함께 DynamoDB 에도 쓴다.

Main Server 가 배포 상태를 읽어 화면에 보여 주고, `sweep` 이 다른 머신에서 만든 배포의 만료를
찾을 수 있게 하기 위해서다. PAWPLOY_STATUS_TABLE 이 없으면 아무것도 하지 않는다 (로컬 전용 모드).

테이블 만들기 (한 번만):
  aws dynamodb create-table --table-name pawploy-deployments \\
    --attribute-definitions AttributeName=deploy_id,AttributeType=S \\
    --key-schema AttributeName=deploy_id,KeyType=HASH --billing-mode PAY_PER_REQUEST

항목 = result.json 내용 그대로 + project_id. 파티션 키는 deploy_id.
"""
import json
import os

from . import awscli

# 리소스가 남아 있지 않다고 보는 상태 (render.REUSABLE_STATUSES 와 같은 기준)
FINISHED_STATUSES = ("destroyed", "failed")


def table() -> str | None:
    return os.environ.get("PAWPLOY_STATUS_TABLE")


def region(default: str) -> str:
    return os.environ.get("PAWPLOY_STATUS_REGION") or default


def put(result: dict, job: dict) -> None:
    """result.json 내용을 한 항목으로 저장한다. 기록 실패는 로그만 남기고 배포 흐름을 멈추지 않는다.

    (여기서 멈추면 리소스는 살아 있는데 아무도 모르는 상태가 된다. 로컬 result.json 은 이미 써 둔 뒤다.)
    """
    if not table() or awscli.offline():
        return
    item = {**result, "project_id": job["project_id"]}
    try:
        awscli.run("dynamodb", "put-item", "--table-name", table(),
                   "--item", json.dumps(to_attr(item)["M"], ensure_ascii=False),
                   region=region(job.get("region") or os.environ.get("PAWPLOY_REGION", "ap-northeast-2")))
    except awscli.AwsError as e:
        print(f"[store] DynamoDB 기록 실패 (계속 진행): {e}", flush=True)


def list_expired(now_iso: str, default_region: str) -> list[dict]:
    """만료 시각이 지났고 아직 끝나지 않은 배포 항목들 (sweep 용). 테이블이 없으면 빈 목록."""
    if not table() or awscli.offline():
        return []
    out = awscli.run(
        "dynamodb", "scan", "--table-name", table(),
        "--filter-expression", "expires_at <= :now AND NOT (#s IN (:destroyed, :failed))",
        "--expression-attribute-names", json.dumps({"#s": "status"}),
        "--expression-attribute-values", json.dumps({
            ":now": {"S": now_iso},
            ":destroyed": {"S": FINISHED_STATUSES[0]},
            ":failed": {"S": FINISHED_STATUSES[1]},
        }),
        region=region(default_region))
    return [from_attr({"M": item}) for item in out.get("Items") or []]


# ---------- Python 값 ↔ DynamoDB AttributeValue ----------

def to_attr(value):
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):          # bool 은 int 의 하위 타입이라 숫자보다 먼저 봐야 한다
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {str(k): to_attr(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [to_attr(v) for v in value]}
    return {"S": str(value)}


def from_attr(attr: dict):
    (kind, value), = attr.items()
    if kind == "NULL":
        return None
    if kind == "N":
        return int(value) if value.lstrip("-").isdigit() else float(value)
    if kind == "M":
        return {k: from_attr(v) for k, v in value.items()}
    if kind == "L":
        return [from_attr(v) for v in value]
    return value  # S, BOOL 등은 그대로

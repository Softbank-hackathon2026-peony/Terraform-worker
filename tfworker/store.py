"""상태 기록: result.json(로컬) 과 함께 DynamoDB 에도 쓴다.

Main Server 가 배포 상태를 읽어 화면에 보여 주고, `sweep` 이 다른 머신에서 만든 배포의 만료를
찾을 수 있게 하기 위해서다. PAWPLOY_STATUS_TABLE 이 없으면 아무것도 하지 않는다 (로컬 전용 모드).

테이블 만들기 (한 번만):
  aws dynamodb create-table --table-name pawploy-deployments \\
    --attribute-definitions AttributeName=deploy_id,AttributeType=S \\
    --key-schema AttributeName=deploy_id,KeyType=HASH --billing-mode PAY_PER_REQUEST

항목 = result.json 내용 그대로 + project_id. 파티션 키는 deploy_id.
같은 테이블에 배포 잠금 항목도 둔다: deploy_id = "lock#<deploy_id>", kind = "lock" (expires_at 이 없어 sweep 이 무시함).

잠금이 필요한 이유: SQS 는 같은 메시지를 두 번 줄 수 있고, sweep·사용자 종료·재시도가 겹칠 수 있다.
워커가 여러 대이면 로컬 result.json 만으로는 "이미 진행 중"을 알 수 없으므로 조건부 쓰기로 한 번에 하나만 돌게 한다.
워커가 중간에 죽어도 잠금은 PAWPLOY_LOCK_LEASE_SEC(기본 3600초) 뒤에 풀린다.
"""
import json
import os
import time

from . import awscli

LOCK_LEASE_SEC = int(os.environ.get("PAWPLOY_LOCK_LEASE_SEC") or 3600)

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
                   region=_region())   # 상태 테이블은 한 곳(서울). 배포 리전(GCP 는 asia-northeast3 등)과 무관
    except awscli.AwsError as e:
        print(f"[store] DynamoDB 기록 실패 (계속 진행): {e}", flush=True)


def _region() -> str:
    return region(os.environ.get("PAWPLOY_REGION", "ap-northeast-2"))


def _key(deploy_id: str) -> str:
    return json.dumps({"deploy_id": {"S": deploy_id}})


def get(deploy_id: str) -> dict | None:
    """DynamoDB 의 배포 결과 (다른 워커가 쓴 것 포함). 테이블이 없으면 None. 조회 실패는 AwsError."""
    if not table() or awscli.offline():
        return None
    out = awscli.run("dynamodb", "get-item", "--table-name", table(), "--key", _key(deploy_id),
                     "--consistent-read", region=_region())
    return from_attr({"M": out["Item"]}) if out.get("Item") else None


def acquire_lock(deploy_id: str, owner: str) -> bool:
    """배포 잠금. 다른 작업이 잡고 있으면 False. 테이블이 없으면(로컬 전용 모드) 항상 True."""
    if not table() or awscli.offline():
        return True
    now = int(time.time())
    item = {"deploy_id": {"S": f"lock#{deploy_id}"}, "kind": {"S": "lock"}, "owner": {"S": owner},
            "lease_until": {"N": str(now + LOCK_LEASE_SEC)}}
    try:
        awscli.run("dynamodb", "put-item", "--table-name", table(), "--item", json.dumps(item),
                   "--condition-expression", "attribute_not_exists(deploy_id) OR lease_until < :now",
                   "--expression-attribute-values", json.dumps({":now": {"N": str(now)}}), region=_region())
        return True
    except awscli.AwsError as e:
        if "ConditionalCheckFailed" in str(e):
            return False
        raise


def release_lock(deploy_id: str, owner: str) -> None:
    """내가 잡은 잠금만 푼다. 실패해도 임대 시간이 지나면 풀리므로 로그만 남긴다."""
    if not table() or awscli.offline():
        return
    try:
        awscli.run("dynamodb", "delete-item", "--table-name", table(), "--key", _key(f"lock#{deploy_id}"),
                   "--condition-expression", "#o = :me",
                   "--expression-attribute-names", json.dumps({"#o": "owner"}),
                   "--expression-attribute-values", json.dumps({":me": {"S": owner}}), region=_region())
    except awscli.AwsError as e:
        print(f"[store] 잠금 해제 실패 (임대 시간 뒤 자동 해제): {e}", flush=True)


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

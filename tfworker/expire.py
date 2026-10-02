"""만료 배포 정리 (1시간 타임아웃의 뒷단). 우리 계정에 배포하므로 "확실히 지워지는 것"이 가장 중요하다.

세 겹으로 지킨다
  1. 예약      배포마다 EventBridge Scheduler 가 expires_at 에 SQS 로 destroy 요청을 보낸다
               (render.py 의 scheduler 블록. PAWPLOY_DESTROY_QUEUE_ARN + PAWPLOY_SCHEDULER_ROLE_ARN 이 있을 때만)
  2. 정기 점검  `python -m tfworker sweep` 을 5~10분마다 돌려 만료됐는데 살아 있는 배포를 destroy 한다
               (로컬 work/ 와 DynamoDB 상태 테이블에서 찾음. 1번이 빠졌거나 실패해도 여기서 잡힌다)
  3. 남은 것    `python -m tfworker orphans` 가 pawploy:expires_at 태그로 만료 지난 리소스를 찾아 알린다
               (워커가 모르는 리소스까지 찾는 마지막 그물. 삭제하지 않고 목록만 낸다)
"""
import calendar
import json
import os
import time

from . import awscli, render, store

DEFAULT_REGION = os.environ.get("PAWPLOY_REGION", "ap-northeast-2")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# 다른 프로세스가 지금 작업 중일 수 있는 상태. 만료 직후라면 끼어들지 않고 다음 점검으로 미룬다
IN_PROGRESS_STATUSES = {"preparing", "generating", "init", "plan", "apply", "health_check", "destroying"}
IN_PROGRESS_GRACE_SEC = 15 * 60   # 헬스체크 최대 7분 + 여유


def now_iso() -> str:
    return time.strftime(TIME_FORMAT, time.gmtime())


def find_expired(now: str | None = None) -> list[dict]:
    """만료 시각이 지났고 리소스가 남아 있을 수 있는 배포 목록. 각 항목: deploy_id, status, expires_at, source."""
    now = now or now_iso()
    found: dict[str, dict] = {}

    for result_file in sorted(render.WORK.glob("*/result.json")):
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        _consider(found, result, "local", now)

    for result in store.list_expired(now, DEFAULT_REGION):
        _consider(found, result, "dynamodb", now)

    return sorted(found.values(), key=lambda d: d["expires_at"])


def _consider(found: dict, result: dict, source: str, now: str) -> None:
    deploy_id, status, expires_at = result.get("deploy_id"), result.get("status"), result.get("expires_at")
    if not deploy_id or not expires_at or status in store.FINISHED_STATUSES:
        return
    if expires_at > now:   # 같은 형식의 UTC 문자열이라 사전순 비교가 시간순 비교와 같다
        return
    if status in IN_PROGRESS_STATUSES and _seconds_between(expires_at, now) < IN_PROGRESS_GRACE_SEC:
        return
    found.setdefault(deploy_id, {"deploy_id": deploy_id, "status": status, "expires_at": expires_at, "source": source})


def _seconds_between(earlier: str, later: str) -> int:
    to_epoch = lambda s: calendar.timegm(time.strptime(s, TIME_FORMAT))
    return to_epoch(later) - to_epoch(earlier)


def find_orphans(region: str, now: str | None = None) -> list[dict]:
    """pawploy:managed 태그가 붙은 리소스 중 pawploy:expires_at 이 지난 것. 각 항목: arn, deploy_id, expires_at.

    IAM 역할처럼 리전이 없는 리소스는 us-east-1 로 조회해야 나온다.
    """
    now = now or now_iso()
    out = awscli.run("resourcegroupstaggingapi", "get-resources",
                     "--tag-filters", "Key=pawploy:managed,Values=true", region=region)
    orphans = []
    for mapping in out.get("ResourceTagMappingList") or []:
        tags = {t["Key"]: t["Value"] for t in mapping.get("Tags") or []}
        expires_at = tags.get("pawploy:expires_at")
        if expires_at and expires_at <= now:
            orphans.append({"arn": mapping["ResourceARN"], "deploy_id": tags.get("pawploy:deploy_id"),
                            "expires_at": expires_at})
    return sorted(orphans, key=lambda o: (o["expires_at"], o["arn"]))

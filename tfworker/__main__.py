"""Terraform Worker 진입점 (파이프라인 21~27단계).

  python -m tfworker deploy <작업.json>              # 21 → 26 → 27 (실패 시 22)
  python -m tfworker destroy <deploy_id> [aws|gcp]   # 삭제 (클라우드 하나만 지울 수도 있음)
  python -m tfworker status <deploy_id>              # 결과 보기 (Main Server 에 돌려줄 내용)
  python -m tfworker sweep [--dry-run]               # 만료된 배포 찾아 삭제 (5~10분마다 정기 실행용)
  python -m tfworker orphans [region]                # 태그로 만료 지난 AWS 리소스 찾기 (알림만, 있으면 exit 1)
  python -m tfworker drain-destroy-queue [queue_url]  # 만료 예약(SQS) 메시지를 모두 받아 destroy (정기 실행용)

Terraform 은 AgentCore 가 만들어 S3 에 둔다(18~19단계). 워커는 사용자가 승인한 클라우드(target)마다
AI 없이 항상 같은 순서로 검증하고 실행한다.
  preparing    ECR 이미지 확인·digest 고정 (AWS)                           image.py
  generating   모듈 가져오기 + 루트 main.tf(provider·태그·backend)          render.py
               IaC 정적 검사                                               iac.py
  init → plan → 정책 검사(policy.py) → S3 보관(artifacts.py) → apply → health_check(26) → running(27)

실패하면 어느 단계든: 앱 로그 수집 → 만든 리소스 정리 → failed 로 보고 (22: failed_stage·log_tail·current_state).
Main Server 가 AgentCore 에 수정을 맡긴 뒤(23~25) 같은 deploy_id 로 다시 요청하면 빈 상태에서 다시 배포한다.
AWS·GCP 를 함께 배포하다 한쪽이 실패해도 성공한 쪽은 그대로 둔다 (전체 상태 partial).

PAWPLOY_STATUS_TABLE 이 있으면 deploy·destroy 는 deploy_id 단위 DynamoDB 잠금을 잡고, 다른 워커가 남긴 결과를
이어받은 뒤 진행한다 (워커 여러 대·SQS 중복 메시지 대비). 입력 오류도 결과에 남긴다 (status=rejected / last_rejection).
"""
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from . import artifacts, awscli, diagnose, expire, health, iac, image, policy, recommendation, store
from . import job as jobmod, render, terraform as tf

HEALTH_TIMEOUT = {"ec2": 420, "lambda": 180, "cloud_run": 180}   # EC2는 부팅 + Docker 설치 시간이 필요
IN_PROGRESS = {"preparing", "generating", "init", "plan", "apply", "health_check"}
# 리소스가 남아 있지 않은 클라우드 상태: 같은 deploy_id 로 그 클라우드를 다시 배포할 수 있다
REUSABLE_TARGET_STATUSES = {None, "failed", "destroyed"}
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class HealthCheckFailed(RuntimeError):
    pass


# ---------------- 결과 기록 (result.json + DynamoDB) ----------------

def load_result(deploy_id: str) -> dict:
    path = render.workdir_for(deploy_id) / "result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return result if isinstance(result.get("targets"), dict) else {}   # 이전 형식(클라우드 구분 없음)은 무시


def aggregate(targets: dict) -> str:
    """클라우드별 상태 → 전체 상태. sweep·store 는 이 값을 본다."""
    s = {t.get("status") for t in targets.values()}
    if s & IN_PROGRESS:
        return "deploying"
    if "destroying" in s:
        return "destroying"
    if "destroy_failed" in s:
        return "destroy_failed"
    if s == {"running"}:
        return "running"
    if "running" in s:
        return "partial"
    if s <= {"destroyed", "failed"}:
        return "destroyed" if "destroyed" in s else "failed"
    return "unknown"


def write_target(job: dict, cloud: str, reset: bool = False, **fields) -> dict:
    """클라우드 하나의 상태를 갱신하고 전체 결과를 다시 쓴다. reset 이면 그 클라우드의 이전 기록을 버린다."""
    deploy_id = job["deploy_id"]
    now = time.strftime(TIME_FORMAT, time.gmtime())
    result = load_result(deploy_id)
    targets = result.get("targets", {})
    entry = {} if reset else targets.get(cloud, {})
    entry.update(fields, updated_at=now)
    targets[cloud] = entry
    result.update(deploy_id=deploy_id, project_id=job["project_id"], expires_at=job["expires_at"],
                  targets=targets, status=aggregate(targets), updated_at=now)
    path = render.workdir_for(deploy_id) / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if "status" in fields:
        print(f"[worker] {cloud}: status={fields['status']} (전체 {result['status']})", flush=True)
    store.put(result, job)   # PAWPLOY_STATUS_TABLE 이 있으면 DynamoDB 에도 (Main Server 가 읽음)
    return result


def _sync_from_store(deploy_id: str) -> dict:
    """다른 워커가 DynamoDB 에 남긴 결과가 더 새로우면 로컬 result.json 으로 가져온다 (컨테이너가 바뀌어도 이어서 진행)."""
    item = store.get(deploy_id)
    local = load_result(deploy_id)
    if item and isinstance(item.get("targets"), dict) and item.get("updated_at", "") > local.get("updated_at", ""):
        path = render.workdir_for(deploy_id) / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")
        return load_result(deploy_id)
    return local


def _reject(raw, e: Exception) -> int:
    """21단계 입력 오류 (종료 코드 2). deploy_id 를 알 수 있으면 결과에도 남겨 Main Server 가 이유를 읽게 한다."""
    print(f"[worker] 입력 오류: {e}")
    deploy_id = str(raw.get("deploy_id") or "").lower() if isinstance(raw, dict) else ""
    if not jobmod.ID_RE.match(deploy_id):
        return 2
    owner = uuid.uuid4().hex
    try:
        if store.acquire_lock(deploy_id, owner):   # 다른 작업이 결과를 쓰는 중이면 덮어쓰지 않도록 기록 생략
            try:
                _sync_from_store(deploy_id)
                _record_rejection(deploy_id, str(raw.get("project_id") or ""), e)
            finally:
                store.release_lock(deploy_id, owner)
    except awscli.AwsError as err:
        print(f"[worker] 입력 오류 기록 실패: {err}")
    return 2


def _record_rejection(deploy_id: str, project_id: str, e: Exception) -> None:
    """기존 배포가 있으면 상태는 그대로 두고 last_rejection 만, 없으면 status=rejected 결과를 만든다."""
    now = time.strftime(TIME_FORMAT, time.gmtime())
    rejection = {"error": str(e), "at": now}
    result = load_result(deploy_id)
    if result.get("targets"):
        result["last_rejection"] = rejection
    else:
        result = {"deploy_id": deploy_id, "project_id": project_id, "status": "rejected", "targets": {},
                  "error": str(e)}
    result["updated_at"] = now
    path = render.workdir_for(deploy_id) / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    store.put(result, {"project_id": result.get("project_id") or project_id})


def _clear_rejection(deploy_id: str) -> None:
    """요청이 받아들여지면 이전 거부 기록(error / last_rejection)을 지운다."""
    result = load_result(deploy_id)
    if "last_rejection" in result or result.get("status") == "rejected":
        result.pop("last_rejection", None)
        result.pop("error", None)
        path = render.workdir_for(deploy_id) / "result.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------- deploy (21 → 26 → 27, 실패 시 22) ----------------

def deploy(job_path: str) -> int:
    raw = None
    try:
        # utf-8-sig: Windows 도구(PowerShell 5.1, 메모장)가 붙이는 BOM 이 있어도 읽는다
        raw = json.loads(Path(job_path).read_text(encoding="utf-8-sig"))
        if raw.get("recommendation_uri"):
            raw = recommendation.merge(raw, recommendation.load(raw["recommendation_uri"]))
        job = jobmod.validate(raw)
    except (ValueError, OSError, awscli.AwsError) as e:
        return _reject(raw, e)

    owner = uuid.uuid4().hex
    try:
        locked = store.acquire_lock(job["deploy_id"], owner)
    except awscli.AwsError as e:
        print(f"[worker] 입력 오류: 상태 저장소(DynamoDB)에 접근할 수 없어 진행하지 않습니다: {e}")
        return 2
    if not locked:
        print(f"[worker] 입력 오류: 같은 deploy_id 의 다른 작업이 진행 중입니다: {job['deploy_id']}")
        return 2
    try:
        return _deploy_locked(job)
    finally:
        store.release_lock(job["deploy_id"], owner)


def _deploy_locked(job: dict) -> int:
    try:
        previous = _sync_from_store(job["deploy_id"])
        for t in job["targets"]:   # 살아 있는 배포 위에 덮어쓰지 않는다
            st = previous.get("targets", {}).get(t["cloud"], {}).get("status")
            if st not in REUSABLE_TARGET_STATUSES:
                raise jobmod.JobError(f"이미 사용 중인 deploy_id 입니다: {job['deploy_id']} ({t['cloud']} status={st}). "
                                      f"먼저 destroy 하거나 실패한 클라우드만 다시 요청하세요")
    except (ValueError, awscli.AwsError) as e:
        print(f"[worker] 입력 오류: {e}")
        _record_rejection(job["deploy_id"], job["project_id"], e)
        return 2
    _clear_rejection(job["deploy_id"])

    # 다른 클라우드가 아직 살아 있으면 만료 시각을 그대로 둔다 (재시도로 1시간 제한이 늘어나지 않게)
    if any(t.get("status") not in REUSABLE_TARGET_STATUSES for t in previous.get("targets", {}).values()):
        job["expires_at"] = previous["expires_at"]

    for t in job["targets"]:
        write_target(job, t["cloud"], reset=True, architecture=t["architecture"], status="preparing")
    statuses = [deploy_target(job, render.target_job(job, t)) for t in job["targets"]]

    result = load_result(job["deploy_id"])
    print(f"\n[worker] 전체 상태: {result['status']}")
    for cloud, t in result["targets"].items():
        print(f"[worker]   {cloud}: {t['status']}  {t.get('endpoint', '') if t['status'] == 'running' else t.get('error', '')}")
    print(f"[worker] 만료 시각(UTC): {result['expires_at']}  →  그 전에 'python -m tfworker destroy {job['deploy_id']}'")
    return 0 if all(s == "running" for s in statuses) else 1


def deploy_target(job: dict, tjob: dict) -> str:
    """클라우드 하나를 배포한다. 돌려주는 값은 최종 상태 (running / failed / destroy_failed)."""
    cloud = tjob["cloud"]
    stage, wd, applied, resource_id = "preparing", None, False, None
    started = time.time()

    def step(name, **fields):
        nonlocal stage
        stage = name
        write_target(job, cloud, status=name, **fields)

    try:
        if cloud == "aws" and not awscli.offline():
            tjob["image_uri"] = image.resolve(tjob["image_uri"])
        step("generating")
        wd = render.render(tjob)
        iac.check(wd / "modules" / "app", cloud, tjob["architecture"])

        step("init")
        tf.run(wd, "init", "-upgrade", *render.backend_args(tjob))

        step("plan")   # plan 결과를 정책(허용 리소스·크기·태그·IAM)으로 검사한 뒤에만 apply 한다
        tf.run(wd, "plan", "-out=tfplan")
        plan = tf.run(wd, "show", "-json", "tfplan", capture_json=True)
        (wd / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        policy.check(plan, tjob["architecture"])
        # 검사를 통과한 최종 코드를 apply 전에 보관 (실패해도 같은 코드로 지울 수 있게)
        artifacts.upload(tjob, wd)

        step("apply")
        applied = True
        tf.run(wd, "apply", "-auto-approve", "tfplan")
        out = tf.run(wd, "output", "-json", capture_json=True)
        resource_id = out["resource_id"]["value"]
        health_url = out["health_url"]["value"]
        step("health_check", endpoint=out["endpoint"]["value"], health_url=health_url,
             resource_id=resource_id, image_uri=tjob["image_uri"])

        timeout = int(os.environ.get("PAWPLOY_HEALTH_TIMEOUT") or HEALTH_TIMEOUT[tjob["architecture"]])
        interval = int(os.environ.get("PAWPLOY_HEALTH_INTERVAL") or 10)
        if not health.wait_healthy(health_url, timeout=timeout, interval=interval):
            raise HealthCheckFailed(f"{health_url} 이 {timeout}초 안에 응답하지 않음")
    except (tf.TerraformError, awscli.AwsError, iac.IacError, policy.PolicyError,
            HealthCheckFailed, KeyError, OSError) as e:
        return _fail(tjob, wd, stage, e, applied, resource_id)

    write_target(job, cloud, status="running", deploy_seconds=int(time.time() - started))
    return "running"


def _fail(tjob: dict, wd: Path | None, stage: str, e: Exception, applied: bool, resource_id: str | None) -> str:
    """22단계 보고: 실패 단계·오류 로그·현재 상태. apply 를 시작했으면 지운 뒤 보고한다."""
    cloud = tjob["cloud"]
    error = f"출력 누락: {e}" if isinstance(e, KeyError) else str(e)
    report = {"failed_stage": stage, "error": error, "log_tail": getattr(e, "output", "")[-4000:]}
    if stage == "health_check":
        report["app_log"] = diagnose.app_log(tjob, resource_id)   # 지우기 전에 앱 로그부터
    print(f"[worker] {cloud}: {stage} 단계 실패 → {error.splitlines()[0]}", flush=True)

    if not applied:   # apply 전 실패: 아직 아무것도 만들지 않았다
        write_target(tjob, cloud, status="failed", destroyed=False, current_state=[], **report)
        return "failed"

    print(f"[worker] {cloud}: 만들어진 리소스 정리 → 수정본은 빈 상태에서 다시 배포", flush=True)
    write_target(tjob, cloud, status="destroying", **report)
    ok = _run_destroy(tjob, wd)
    write_target(tjob, cloud, status="failed" if ok else "destroy_failed", destroyed=ok,
                 current_state=_state_list(wd))
    return "failed" if ok else "destroy_failed"


def _run_destroy(tjob: dict, wd: Path) -> bool:
    try:
        tf.run(wd, "destroy", "-auto-approve")
        return True
    except tf.TerraformError as e:
        write_target(tjob, tjob["cloud"], destroy_error=str(e), destroy_log_tail=e.output[-4000:])
        return False


def _state_list(wd: Path) -> list[str]:
    """현재 state 에 남은 리소스 주소. 정리가 끝났으면 빈 목록."""
    try:
        return [line for line in tf.run(wd, "state", "list", capture_text=True).splitlines() if line.strip()]
    except tf.TerraformError as e:
        return [f"(state 를 읽지 못함: {e})"]


# ---------------- destroy ----------------

def destroy(deploy_id: str, cloud: str | None = None) -> int:
    owner = uuid.uuid4().hex
    try:
        locked = store.acquire_lock(deploy_id, owner)
    except awscli.AwsError as e:
        print(f"[worker] 상태 저장소(DynamoDB)에 접근할 수 없어 삭제를 미룹니다: {e}")
        return 1
    if not locked:
        print(f"[worker] 같은 deploy_id 의 다른 작업이 진행 중이라 삭제를 미룹니다: {deploy_id} (다음 점검에서 다시 시도)")
        return 1
    try:
        return _destroy_locked(deploy_id, cloud)
    finally:
        store.release_lock(deploy_id, owner)


def _destroy_locked(deploy_id: str, cloud: str | None) -> int:
    root = render.workdir_for(deploy_id)
    if not any(root.glob("*/job.json")):
        try:
            artifacts.download(deploy_id, root)   # 다른 머신에서 배포한 경우
        except awscli.AwsError as e:
            print(f"[worker] S3에서 작업 폴더를 받지 못했습니다: {e}")
    dirs = sorted(p.parent for p in root.glob("*/job.json") if cloud in (None, p.parent.name))
    if not dirs:
        print(f"[worker] 배포를 찾을 수 없습니다: {deploy_id}{'/' + cloud if cloud else ''}")
        return 2

    try:
        result = _sync_from_store(deploy_id)
    except awscli.AwsError as e:
        print(f"[worker] DynamoDB 결과를 읽지 못해 로컬 결과로 진행합니다: {e}")
        result = load_result(deploy_id)
    all_ok = True
    for wd in dirs:
        tjob = json.loads((wd / "job.json").read_text(encoding="utf-8"))
        c = tjob["cloud"]
        if result.get("targets", {}).get(c, {}).get("status") in ("failed", "destroyed"):
            print(f"[worker] {c}: 지울 리소스 없음 (이미 정리됨)")
            continue
        # state가 없으면 terraform은 "지울 것이 없다"며 성공해 버린다. 리소스가 남지 않도록 막는다
        if not tjob.get("state_bucket") and not (wd / "terraform.tfstate").exists():
            write_target(tjob, c, status="destroy_failed",
                         error="state를 찾을 수 없어 삭제 대상을 알 수 없습니다 (로컬 state가 없고 S3 backend도 아님)")
            all_ok = False
            continue
        write_target(tjob, c, status="destroying")
        try:
            tf.run(wd, "init", *render.backend_args(tjob))
        except tf.TerraformError as e:
            write_target(tjob, c, status="destroy_failed", error=str(e), log_tail=e.output[-4000:])
            all_ok = False
            continue
        ok = _run_destroy(tjob, wd)
        write_target(tjob, c, status="destroyed" if ok else "destroy_failed", destroyed=ok,
                     current_state=_state_list(wd))
        all_ok = all_ok and ok
    return 0 if all_ok else 1


def status(deploy_id: str) -> int:
    path = render.workdir_for(deploy_id) / "result.json"
    if not path.exists():
        print(f"[worker] 결과가 없습니다: {deploy_id}")
        return 2
    print(path.read_text(encoding="utf-8"))
    return 0


# ---------------- 만료 정리 ----------------

def sweep(dry_run: bool) -> int:
    """만료됐는데 살아 있는 배포를 모두 destroy 한다. 정기 실행(크론 등) 용."""
    expired = expire.find_expired()
    if not expired:
        print("[sweep] 만료된 배포 없음")
        return 0
    failed = []
    for d in expired:
        print(f"[sweep] 만료 {d['deploy_id']}: expires_at={d['expires_at']} status={d['status']} ({d['source']})")
        if not dry_run and destroy(d["deploy_id"]) != 0:
            failed.append(d["deploy_id"])
    if dry_run:
        print(f"[sweep] --dry-run: {len(expired)}개를 지우지 않았습니다")
        return 0
    if failed:
        print(f"[sweep] 삭제 실패: {', '.join(failed)}")
        return 1
    print(f"[sweep] {len(expired)}개 삭제 완료")
    return 0


def drain_destroy_queue(queue_url: str | None) -> int:
    """만료 예약(EventBridge Scheduler → SQS) 메시지를 큐가 빌 때까지 받아 destroy 한다. 정기 실행(sweep 과 함께) 용.

    성공하거나 지울 배포가 없으면(종료 코드 0·2) 메시지를 지운다. 실패하면 남겨 두어 visibility timeout 뒤 다시 받는다
    (sweep 이 같은 배포를 따로 잡아도 destroy 는 여러 번 해도 안전하다).
    """
    queue_url = queue_url or os.environ.get("PAWPLOY_DESTROY_QUEUE_URL")
    if not queue_url:
        print("[queue] 큐 주소가 없습니다 (인자 또는 PAWPLOY_DESTROY_QUEUE_URL)")
        return 2
    m = re.match(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com/", queue_url)
    region = m.group(1) if m else expire.DEFAULT_REGION
    handled = failed = 0
    while True:
        out = awscli.run("sqs", "receive-message", "--queue-url", queue_url, "--max-number-of-messages", "10",
                         "--wait-time-seconds", "1", "--visibility-timeout", "1800", region=region)
        messages = out.get("Messages") or []
        if not messages:
            break
        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                deploy_id = body["deploy_id"] if body.get("action") == "destroy" else None
            except (ValueError, KeyError, TypeError):
                deploy_id = None
            if not deploy_id or not jobmod.ID_RE.match(str(deploy_id)):
                print(f"[queue] 알 수 없는 메시지 → 삭제: {msg.get('Body', '')[:200]}")
                rc = 0
            else:
                print(f"[queue] 만료 destroy 요청: {deploy_id}")
                rc = destroy(deploy_id)
            if rc in (0, 2):
                awscli.run("sqs", "delete-message", "--queue-url", queue_url,
                           "--receipt-handle", msg["ReceiptHandle"], region=region)
                handled += 1
            else:
                failed += 1
    print(f"[queue] 처리 {handled}건, 재시도 대기 {failed}건")
    return 1 if failed else 0


def orphans(region: str) -> int:
    """pawploy 태그가 붙었는데 만료 시각이 지난 리소스를 찾아 보여 준다. 지우지는 않는다. (AWS 만)"""
    try:
        found = expire.find_orphans(region)
    except awscli.AwsError as e:
        print(f"[orphans] 조회 실패: {e}")
        return 2
    if not found:
        print(f"[orphans] {region}: 만료 지난 리소스 없음")
        return 0
    for o in found:
        print(f"[orphans] {o['arn']}  deploy_id={o['deploy_id']} expires_at={o['expires_at']}")
    print(f"[orphans] {len(found)}개. 삭제하지 않았습니다 → 'python -m tfworker destroy <deploy_id>' 로 정리하세요")
    return 1   # 정기 실행에서 알림 조건으로 쓰도록 0 이 아닌 코드


def main(argv: list[str]) -> int:
    cmd, args = (argv[0] if argv else None), argv[1:]
    if cmd == "sweep" and not set(args) - {"--dry-run"}:
        return sweep("--dry-run" in args)
    if cmd == "drain-destroy-queue" and len(args) <= 1:
        return drain_destroy_queue(args[0] if args else None)
    if cmd == "orphans" and len(args) <= 1:
        return orphans(args[0] if args else expire.DEFAULT_REGION)
    if cmd == "destroy" and len(args) in (1, 2) and (len(args) == 1 or args[1] in jobmod.CLOUD_ARCHITECTURES):
        return destroy(*args)
    if cmd in {"deploy", "status"} and len(args) == 1:
        return {"deploy": deploy, "status": status}[cmd](args[0])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

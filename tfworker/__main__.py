"""Terraform Worker 진입점.

  python -m tfworker deploy examples/job-ec2.json     # 배포
  python -m tfworker destroy <deploy_id>               # 삭제
  python -m tfworker status <deploy_id>                # 결과 보기
  python -m tfworker sweep [--dry-run]                 # 만료된 배포 찾아 삭제 (5~10분마다 정기 실행용)
  python -m tfworker orphans [region]                  # 태그로 만료 지난 리소스 찾기 (알림만, 있으면 exit 1)

배포 단계 (AI 없이 항상 같은 결과를 내는 코드)
  2. 추천 결과 읽기   recommendation.py  (작업 입력에 recommendation_uri가 있을 때)
     입력 검증        job.py
  1. ECR 이미지 확인  image.py           (태그 → digest 고정)
  3. Terraform 생성   render.py          (모듈 복사 + 변수 파일)
  4. S3 보관          artifacts.py       (init 뒤에 해서 provider 잠금 파일까지 저장)
  5. 적용            terraform.py       (plan → 정책 검사 policy.py → apply → output)
  실패·응답 없음이면 diagnose.py가 진단 자료를 모으고 AgentCore 진단 에이전트를 부른다.
  상태는 result.json 과 DynamoDB(store.py)에 기록하고, 만료 정리는 expire.py 가 맡는다.
"""
import json
import os
import sys
import time
from pathlib import Path

from . import artifacts, awscli, diagnose, expire, health, image, policy, recommendation, store
from . import job as jobmod, render, terraform as tf

HEALTH_TIMEOUT = {"ec2": 420, "lambda": 180}   # EC2는 부팅 + Docker 설치 시간이 필요


def _health_params(architecture: str) -> tuple[int, int]:
    """헬스체크 (최대 대기 초, 재시도 간격 초). 테스트에서는 환경변수로 짧게 줄인다."""
    timeout = int(os.environ.get("PAWPLOY_HEALTH_TIMEOUT") or HEALTH_TIMEOUT[architecture])
    interval = int(os.environ.get("PAWPLOY_HEALTH_INTERVAL") or 10)
    return timeout, interval


def write_result(job: dict, **fields) -> dict:
    path = render.workdir_for(job["deploy_id"]) / "result.json"
    result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    result.update({"deploy_id": job["deploy_id"], "architecture": job["architecture"],
                   "expires_at": job["expires_at"], **fields,
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[worker] status={result.get('status')}", flush=True)
    store.put(result, job)   # PAWPLOY_STATUS_TABLE 이 있으면 DynamoDB 에도 (Main Server 가 읽음)
    return result


def deploy(job_path: str) -> int:
    # 2단계: 작업 입력 + AgentCore 추천 결과 → 검증
    try:
        raw = json.loads(Path(job_path).read_text(encoding="utf-8"))
        if raw.get("recommendation_uri"):
            raw = recommendation.merge(raw, recommendation.load(raw["recommendation_uri"]))
        job = jobmod.validate(raw)
        render.check_not_active(job["deploy_id"])   # 살아 있는 배포 위에 덮어쓰지 않는다
    except (ValueError, OSError, awscli.AwsError) as e:
        print(f"[worker] 입력 오류: {e}")
        return 2

    render.workdir_for(job["deploy_id"]).mkdir(parents=True, exist_ok=True)
    write_result(job, status="preparing")
    started = time.time()

    # 1단계: ECR 이미지 확인
    if awscli.offline():
        print("[image] PAWPLOY_OFFLINE=1 → 이미지 확인 건너뜀", flush=True)
    else:
        try:
            job["image_uri"] = image.resolve(job["image_uri"])
        except awscli.AwsError as e:
            write_result(job, status="failed", error=str(e))
            return 2

    # 3단계: Terraform 코드 생성
    write_result(job, status="generating")
    wd = render.render(job)

    applied = False   # apply 를 시작했는지. 그 전에 실패하면 지울 리소스가 없다
    try:
        write_result(job, status="init")
        tf.run(wd, "init", "-upgrade", *render.backend_args(job))

        # 4단계: 생성된 코드 S3 보관 (apply 전에 해야 실패해도 같은 코드로 지울 수 있음)
        artifacts.upload(job, wd)

        # 5단계: 적용. plan 결과를 정책(허용 리소스·크기·태그)으로 검사한 뒤에만 apply 한다
        write_result(job, status="plan")
        tf.run(wd, "plan", "-out=tfplan")
        plan = tf.run(wd, "show", "-json", "tfplan", capture_json=True)
        (wd / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        policy.check(plan, job["architecture"])

        write_result(job, status="apply")
        applied = True
        tf.run(wd, "apply", "-auto-approve", "tfplan")
        out = tf.run(wd, "output", "-json", capture_json=True)
    except (tf.TerraformError, awscli.AwsError, policy.PolicyError) as e:
        result = write_result(job, status="failed", error=str(e),
                              log_tail=getattr(e, "output", "")[-4000:])
        _diagnose(job, wd, result)
        if not applied:
            # init/plan 단계 실패: 아직 아무것도 만들지 않았다. 여기서 destroy 를 돌리면
            # init 실패 시 destroy 도 실패해 destroy_failed 가 되고 그 deploy_id 를 다시 못 쓴다
            print("[worker] apply 전 실패 → 만들어진 리소스 없음, 정리 생략")
            return 1
        print("[worker] 배포 실패 → 만들어진 리소스 정리 시도")
        _destroy(job, wd, final_status="failed")
        return 1

    endpoint = out["endpoint"]["value"]
    health_url = out["health_url"]["value"]
    write_result(job, status="health_check", endpoint=endpoint, health_url=health_url,
                 resource_id=out["resource_id"]["value"])

    timeout, interval = _health_params(job["architecture"])
    ok = health.wait_healthy(health_url, timeout=timeout, interval=interval)
    result = write_result(job, status="running" if ok else "unhealthy",
                          deploy_seconds=int(time.time() - started))
    if not ok:
        _diagnose(job, wd, result)

    print(f"\n[worker] 접속 주소: {endpoint}")
    print(f"[worker] 만료 시각(UTC): {job['expires_at']}  →  그 전에 'python -m tfworker destroy {job['deploy_id']}'")
    return 0 if ok else 3


def _diagnose(job: dict, wd: Path, result: dict) -> None:
    diagnosis = diagnose.run(job, wd, result)
    if diagnosis:
        write_result(job, diagnosis=diagnosis)


def _destroy(job: dict, wd: Path, final_status: str = "destroyed") -> bool:
    try:
        write_result(job, status="destroying")
        tf.run(wd, "destroy", "-auto-approve")
        write_result(job, status=final_status, destroyed=True)
        return True
    except tf.TerraformError as e:
        write_result(job, status="destroy_failed", error=str(e), log_tail=e.output[-4000:])
        return False


def destroy(deploy_id: str) -> int:
    wd = render.workdir_for(deploy_id)
    job_file = wd / "job.json"
    if not job_file.exists():
        try:
            found = artifacts.download(deploy_id, wd)   # 다른 머신에서 배포한 경우
        except awscli.AwsError as e:
            print(f"[worker] S3에서 작업 폴더를 받지 못했습니다: {e}")
            found = False
        if not found:
            print(f"[worker] 배포를 찾을 수 없습니다: {deploy_id}")
            return 2
    job = json.loads(job_file.read_text(encoding="utf-8"))

    # state가 없으면 terraform은 "지울 것이 없다"며 성공해 버린다. 리소스가 남지 않도록 막는다
    if not job.get("state_bucket") and not (wd / "terraform.tfstate").exists():
        write_result(job, status="destroy_failed",
                     error="state를 찾을 수 없어 삭제 대상을 알 수 없습니다 (로컬 state가 없고 S3 backend도 아님)")
        return 1

    try:
        tf.run(wd, "init", *render.backend_args(job))
    except tf.TerraformError as e:
        write_result(job, status="destroy_failed", error=str(e), log_tail=e.output[-4000:])
        return 1
    return 0 if _destroy(job, wd) else 1


def status(deploy_id: str) -> int:
    path = render.workdir_for(deploy_id) / "result.json"
    if not path.exists():
        print(f"[worker] 결과가 없습니다: {deploy_id}")
        return 2
    print(path.read_text(encoding="utf-8"))
    return 0


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


def orphans(region: str) -> int:
    """pawploy 태그가 붙었는데 만료 시각이 지난 리소스를 찾아 보여 준다. 지우지는 않는다."""
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
    if cmd == "orphans" and len(args) <= 1:
        return orphans(args[0] if args else expire.DEFAULT_REGION)
    if cmd in {"deploy", "destroy", "status"} and len(args) == 1:
        return {"deploy": deploy, "destroy": destroy, "status": status}[cmd](args[0])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

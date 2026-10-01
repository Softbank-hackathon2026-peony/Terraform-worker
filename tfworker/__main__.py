"""Terraform Worker 진입점.

  python -m tfworker deploy examples/job-ec2.json     # 배포
  python -m tfworker destroy <deploy_id>               # 삭제
  python -m tfworker status <deploy_id>                # 결과 보기

배포 단계 (AI 없이 항상 같은 결과를 내는 코드)
  2. 추천 결과 읽기   recommendation.py  (작업 입력에 recommendation_uri가 있을 때)
     입력 검증        job.py
  1. ECR 이미지 확인  image.py           (태그 → digest 고정)
  3. Terraform 생성   render.py          (모듈 복사 + 변수 파일)
  4. S3 보관          artifacts.py       (init 뒤에 해서 provider 잠금 파일까지 저장)
  5. 적용            terraform.py       (plan → apply → output)
  실패·응답 없음이면 diagnose.py가 진단 자료를 모으고 AgentCore 진단 에이전트를 부른다.
"""
import json
import os
import sys
import time
from pathlib import Path

from . import artifacts, awscli, diagnose, health, image, recommendation
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

    try:
        write_result(job, status="init")
        tf.run(wd, "init", "-upgrade", *render.backend_args(job))

        # 4단계: 생성된 코드 S3 보관 (apply 전에 해야 실패해도 같은 코드로 지울 수 있음)
        artifacts.upload(job, wd)

        # 5단계: 적용
        write_result(job, status="plan")
        tf.run(wd, "plan", "-out=tfplan")

        write_result(job, status="apply")
        tf.run(wd, "apply", "-auto-approve", "tfplan")
        out = tf.run(wd, "output", "-json", capture_json=True)
    except (tf.TerraformError, awscli.AwsError) as e:
        result = write_result(job, status="failed", error=str(e),
                              log_tail=getattr(e, "output", "")[-4000:])
        _diagnose(job, wd, result)
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


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"deploy", "destroy", "status"}:
        print(__doc__)
        return 2
    cmd, arg = argv
    return {"deploy": deploy, "destroy": destroy, "status": status}[cmd](arg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

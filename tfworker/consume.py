"""SQS 작업 큐 소비자 (ECS Fargate 서비스로 상시 실행).

  python -m tfworker consume [queue_url] [--once]

메시지 (FIFO 큐, MessageGroupId = deploy_id → 같은 배포의 요청은 순서대로 처리)
  {"action": "deploy",  "job_uri": "s3://<버킷>/jobs/<deploy_id>/<요청>.json"}
  {"action": "destroy", "deploy_id": "dep-demo", "cloud": "gcp"}      # cloud 는 생략 가능(전체 삭제)

처리 규칙
  - 메시지 하나씩 처리. 처리하는 동안 가시성 시간을 계속 연장하고(다른 워커가 같은 메시지를 받지 않게),
    ECS 작업 보호를 켜서 배포 중에 ECS 가 이 작업을 종료하지 않게 한다.
  - deploy 는 결과(성공·실패·입력 오류)가 DynamoDB 에 남으므로 메시지를 지운다. 재시도는 Main 이 판단한다.
    예외로 죽은 경우만 남겨 두어 다시 받는다 (3회 실패하면 DLQ).
  - destroy 는 성공(0)·지울 것 없음(2)이면 지우고, 실패(1: 다른 작업 중 등)면 남겨 두어 다시 시도한다.
  - PAWPLOY_MAINTENANCE_INTERVAL_SEC(기본 300초)마다 maintenance(만료 큐 + sweep)를 돌린다.
  - SIGTERM 을 받으면 새 메시지는 받지 않고, 하던 작업만 마치고 끝낸다.
  - S3 state·작업 폴더 보관이 켜져 있으면, 작업이 끝난 로컬 폴더는 지운다 (컨테이너 디스크 보호).
"""
import json
import os
import re
import shutil
import signal
import threading
import time
import urllib.request

from . import awscli, expire, render

VISIBILITY_SEC = 900          # 받을 때 15분, 처리 중에는 5분마다 다시 15분으로 연장
HEARTBEAT_SEC = 300
WAIT_SEC = 20                 # long polling


class Consumer:
    def __init__(self, queue_url: str, once: bool = False):
        m = re.match(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com/", queue_url)
        self.queue_url = queue_url
        self.region = m.group(1) if m else expire.DEFAULT_REGION
        self.once = once
        self.stopping = False
        self.maintenance_every = int(os.environ.get("PAWPLOY_MAINTENANCE_INTERVAL_SEC") or 300)
        self.last_maintenance = 0.0

    # ---------------- 루프 ----------------

    def run(self) -> int:
        from .__main__ import maintenance
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        print(f"[consume] 시작: {self.queue_url} (maintenance {self.maintenance_every}초마다)", flush=True)
        while not self.stopping:
            if time.time() - self.last_maintenance >= self.maintenance_every and not self.once:
                self.last_maintenance = time.time()
                try:
                    maintenance()
                except Exception as e:   # 정기 작업 실패가 큐 처리를 멈추면 안 됨
                    print(f"[consume] maintenance 실패: {type(e).__name__}: {e}", flush=True)
            try:
                out = awscli.run("sqs", "receive-message", "--queue-url", self.queue_url,
                                 "--max-number-of-messages", "1", "--wait-time-seconds", str(0 if self.once else WAIT_SEC),
                                 "--visibility-timeout", str(VISIBILITY_SEC), region=self.region)
            except awscli.AwsError as e:
                print(f"[consume] 큐 읽기 실패, 잠시 뒤 재시도: {e}", flush=True)
                if self.once:
                    return 1
                time.sleep(10)
                continue
            messages = out.get("Messages") or []
            if not messages and self.once:
                break
            for msg in messages:
                self.handle(msg)
        print("[consume] 종료", flush=True)
        return 0

    def _stop(self, signum, frame):
        print(f"[consume] 종료 신호({signum}) → 하던 작업만 마치고 끝냄", flush=True)
        self.stopping = True

    # ---------------- 메시지 하나 ----------------

    def handle(self, msg: dict) -> None:
        from .__main__ import deploy, destroy
        receipt = msg["ReceiptHandle"]
        try:
            body = json.loads(msg["Body"])
            action = body.get("action")
            if action == "deploy" and str(body.get("job_uri", "")).startswith("s3://"):
                label = body["job_uri"]
            elif action == "destroy" and body.get("deploy_id"):
                label = f"{body['deploy_id']}" + (f"/{body['cloud']}" if body.get("cloud") else "")
            else:
                raise ValueError("알 수 없는 action 또는 필수 값 없음")
        except (ValueError, TypeError, KeyError) as e:
            print(f"[consume] 잘못된 메시지 → 삭제 ({e}): {str(msg.get('Body'))[:200]}", flush=True)
            self._delete(receipt)
            return

        print(f"[consume] ▶ {action} {label}", flush=True)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(receipt, stop_heartbeat), daemon=True)
        heartbeat.start()
        protected = _task_protection(True)
        crashed = False
        try:
            rc = deploy(body["job_uri"]) if action == "deploy" else destroy(body["deploy_id"], body.get("cloud"))
        except Exception as e:   # 워커 버그·일시적 장애: 메시지를 남겨 다시 받는다
            print(f"[consume] 처리 중 예외: {type(e).__name__}: {e}", flush=True)
            rc, crashed = 1, True
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=5)
            if protected:
                _task_protection(False)

        keep = crashed or (action == "destroy" and rc == 1)
        print(f"[consume] ■ {action} {label} → 종료 코드 {rc}, 메시지 {'남김(재시도)' if keep else '삭제'}", flush=True)
        if not keep:
            self._delete(receipt)
        self._cleanup(body if action == "destroy" else None)

    def _heartbeat(self, receipt: str, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SEC):
            try:
                awscli.run("sqs", "change-message-visibility", "--queue-url", self.queue_url,
                           "--receipt-handle", receipt, "--visibility-timeout", str(VISIBILITY_SEC),
                           region=self.region, json_output=False)
            except awscli.AwsError as e:
                print(f"[consume] 가시성 연장 실패: {e}", flush=True)

    def _delete(self, receipt: str) -> None:
        try:
            awscli.run("sqs", "delete-message", "--queue-url", self.queue_url, "--receipt-handle", receipt,
                       region=self.region, json_output=False)
        except awscli.AwsError as e:
            print(f"[consume] 메시지 삭제 실패 (다시 받으면 잠금·상태 검사로 중복 실행은 막힘): {e}", flush=True)

    def _cleanup(self, body: dict | None) -> None:
        """state·코드가 S3 에 있을 때만 로컬 작업 폴더를 지운다 (로컬 state 를 지우면 destroy 를 못 함)."""
        if not (os.environ.get("PAWPLOY_STATE_BUCKET") and os.environ.get("PAWPLOY_ARTIFACT_BUCKET")):
            return
        for wd in render.WORK.iterdir() if render.WORK.exists() else []:
            if wd.is_dir():
                shutil.rmtree(wd, ignore_errors=True)


def _task_protection(enabled: bool) -> bool:
    """ECS 에서 돌 때만: 작업 보호를 켜고 끈다. 배포 중에 서비스 갱신·축소로 작업이 종료되지 않게."""
    meta = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not meta:
        return False
    try:
        with urllib.request.urlopen(f"{meta}/task", timeout=3) as r:
            task = json.loads(r.read())
        args = ["ecs", "update-task-protection", "--cluster", task["Cluster"], "--tasks", task["TaskARN"]]
        args += ["--protection-enabled", "--expires-in-minutes", "120"] if enabled else ["--no-protection-enabled"]
        awscli.run(*args, region=task["TaskARN"].split(":")[3])
        return True
    except Exception as e:
        print(f"[consume] 작업 보호 {'켜기' if enabled else '끄기'} 실패 (계속 진행): {e}", flush=True)
        return False


def main(args: list[str]) -> int:
    once = "--once" in args
    rest = [a for a in args if a != "--once"]
    queue_url = rest[0] if rest else os.environ.get("PAWPLOY_JOBS_QUEUE_URL")
    if not queue_url:
        print("[consume] 큐 주소가 없습니다 (인자 또는 PAWPLOY_JOBS_QUEUE_URL)")
        return 2
    return Consumer(queue_url, once=once).run()

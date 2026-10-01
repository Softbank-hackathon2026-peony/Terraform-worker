"""aws CLI 실행 도우미.

Python 표준 라이브러리만 쓰기 위해 boto3 대신 aws CLI를 부른다.
PAWPLOY_OFFLINE=1이면 AWS를 부르는 단계(이미지 확인, S3 보관, 진단 로그 수집)를 건너뛴다.
가짜 terraform으로 흐름만 시험할 때 쓴다.
"""
import json
import os
import shutil
import subprocess

AWS = os.environ.get("AWS_BIN", "aws")


class AwsError(RuntimeError):
    pass


def offline() -> bool:
    return os.environ.get("PAWPLOY_OFFLINE") == "1"


def run(*args: str, region: str | None = None, json_output: bool = True):
    if shutil.which(AWS) is None:
        raise AwsError("aws 실행 파일을 찾을 수 없습니다. AWS CLI를 설치하거나 AWS_BIN을 지정하세요")

    cmd = [AWS, *args]
    if region:
        cmd += ["--region", region]
    if json_output:
        cmd += ["--output", "json"]
    print(f"[aws] $ aws {' '.join(args[:2])} ...", flush=True)

    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise AwsError(f"aws {' '.join(args[:2])} 실패: {p.stderr.strip()[-1000:]}")
    if not json_output:
        return p.stdout
    return json.loads(p.stdout) if p.stdout.strip() else {}

"""2단계: AgentCore 추천 결과 읽기.

분석·추천 에이전트(AgentCore)가 S3에 저장한 결과 JSON을 읽어 작업 입력에 합친다.
AI가 만든 값이므로 정해진 필드만 골라 오고, 검사는 job.py가 다른 입력과 똑같이 한다.
"""
import json
from pathlib import Path

from . import awscli
from .job import JobError

FIELDS = ("architecture", "container_port", "size", "health_path", "env")


def load(uri: str) -> dict:
    """s3://버킷/경로 또는 로컬 파일 경로에서 추천 결과를 읽는다."""
    if uri.startswith("s3://"):
        text = awscli.run("s3", "cp", uri, "-", json_output=False)
    else:
        text = Path(uri).read_text(encoding="utf-8")
    # Windows 도구가 붙이는 BOM 은 S3·로컬 어느 쪽에서 읽어도 맨 앞에 남으므로 떼어 낸다
    data = json.loads(text.lstrip("﻿"))
    if not isinstance(data, dict):
        raise JobError("추천 결과는 JSON 객체여야 합니다")
    return data


def merge(raw_job: dict, rec: dict) -> dict:
    """추천 값 위에 작업 입력 값을 덮어쓴다. Main Server가 직접 지정한 값이 우선이다."""
    rec_env = rec.get("env") or {}
    job_env = raw_job.get("env") or {}
    if not isinstance(rec_env, dict) or not isinstance(job_env, dict):
        raise JobError("env는 {이름: 값} 형태여야 합니다")

    merged = {k: rec[k] for k in FIELDS if rec.get(k) not in (None, "")}
    merged.update({k: v for k, v in raw_job.items() if k != "recommendation_uri"})
    merged["env"] = {**rec_env, **job_env}
    return merged

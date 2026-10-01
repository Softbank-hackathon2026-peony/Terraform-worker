"""배포 작업(job) 입력 검증.

Main Server가 보내는 작업 JSON 예시는 examples/job-ec2.json 참고.
"""
import re
from datetime import datetime, timedelta, timezone

ARCHITECTURES = {"ec2", "lambda"}          # 지금 지원하는 아키텍처
SIZES = {"micro", "small", "medium"}       # 허용 크기 (악용 방지: large 이상 없음)
DEFAULT_TTL_MINUTES = 60                   # 1시간 뒤 자동 종료

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,38}[a-z0-9]$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ECR_IMAGE_RE = re.compile(r"^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9._/-]+(:[\w.-]+|@sha256:[0-9a-f]{64})$")


class JobError(ValueError):
    pass


def validate(raw: dict) -> dict:
    """작업 JSON을 검사하고, 기본값을 채운 정규화된 dict를 돌려준다."""
    def need(key):
        if key not in raw or raw[key] in (None, ""):
            raise JobError(f"필수 값이 없습니다: {key}")
        return raw[key]

    deploy_id = str(need("deploy_id")).lower()
    if not ID_RE.match(deploy_id):
        raise JobError("deploy_id는 소문자·숫자·하이픈 4~40자여야 합니다")

    project_id = str(need("project_id"))

    architecture = str(need("architecture")).lower()
    if architecture not in ARCHITECTURES:
        raise JobError(f"지원하지 않는 아키텍처입니다: {architecture} (가능: {sorted(ARCHITECTURES)})")

    image_uri = str(need("image_uri"))
    if not ECR_IMAGE_RE.match(image_uri):
        raise JobError("image_uri는 ECR 주소(태그 또는 @sha256 digest 포함)여야 합니다")

    port = _int(raw.get("container_port", 8080), "container_port")
    if not 1 <= port <= 65535:
        raise JobError("container_port가 올바르지 않습니다")

    size = str(raw.get("size", "small")).lower()
    if size not in SIZES:
        raise JobError(f"허용되지 않는 크기입니다: {size} (가능: {sorted(SIZES)})")

    health_path = str(raw.get("health_path", "/"))
    if not health_path.startswith("/"):
        health_path = "/" + health_path

    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise JobError("env는 {이름: 값} 형태여야 합니다")
    for k, v in env.items():
        if not ENV_KEY_RE.match(k):
            raise JobError(f"환경변수 이름이 올바르지 않습니다: {k}")
        if "\n" in str(v):
            raise JobError(f"환경변수 값에 줄바꿈을 넣을 수 없습니다: {k}")
    env = {k: str(v) for k, v in env.items()}

    region = str(raw.get("region") or _region_from_image(image_uri))

    ttl = _int(raw.get("ttl_minutes", DEFAULT_TTL_MINUTES), "ttl_minutes")
    ttl = max(5, min(ttl, DEFAULT_TTL_MINUTES))            # 최대 1시간
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "deploy_id": deploy_id,
        "project_id": project_id,
        "architecture": architecture,
        "image_uri": image_uri,
        "container_port": port,
        "size": size,
        "health_path": health_path,
        "env": env,
        "region": region,
        "expires_at": expires_at,
    }


def _int(value, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise JobError(f"{key}는 숫자여야 합니다: {value!r}") from None


def _region_from_image(image_uri: str) -> str:
    m = re.search(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", image_uri)
    if not m:
        raise JobError("region을 알 수 없습니다")
    return m.group(1)

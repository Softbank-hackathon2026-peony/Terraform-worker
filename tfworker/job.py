"""배포 작업(job) 입력 검증 (파이프라인 21단계: Main Server → Worker).

사용자가 승인한 클라우드마다 target 하나. AWS·GCP 를 함께 고르면 target 이 두 개다.

  {
    "deploy_id": "dep-demo", "project_id": "prj_demo",
    "container_port": 8080, "size": "small", "health_path": "/", "env": {...},
    "targets": [
      {"cloud": "aws", "architecture": "ec2", "image_uri": "<ECR>@sha256:...",
       "terraform_uri": "s3://<버킷>/<경로>/aws/"},
      {"cloud": "gcp", "architecture": "cloud_run", "image_uri": "<Artifact Registry>@sha256:...",
       "terraform_uri": "s3://<버킷>/<경로>/gcp/"}
    ]
  }

architecture 는 생략 가능 (AgentCore 모듈에 들어 있는 리소스로 워커가 판단). 넘기면 모듈과 다를 때 실패로 보고한다.
terraform_uri 는 AgentCore 가 만든 모듈(18~19단계)의 위치. 보통 생략하고 PAWPLOY_AGENT_BUCKET 규칙
(projects/<project_id>/deploy/<deploy_id>/attempt-<N>/<cloud>/)에서 최신 attempt 를 쓴다. 둘 다 없으면 기본 모듈.
targets 없이 architecture·image_uri 를 최상위에 두면 AWS target 하나로 본다 (이전 입력 형식).
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLOUD_ARCHITECTURES = {                    # 지금 지원하는 클라우드별 아키텍처
    "aws": {"ec2", "lambda"},
    "gcp": {"cloud_run"},
}
DEFAULT_ARCHITECTURE = {"aws": "ec2", "gcp": "cloud_run"}   # architecture 도 AgentCore 모듈도 없을 때 쓸 기본 모듈
SIZES = {"micro", "small", "medium"}       # 허용 크기 (악용 방지: large 이상 없음)
DEFAULT_TTL_MINUTES = 60                   # 1시간 뒤 자동 종료

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,38}[a-z0-9]$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ECR_IMAGE_RE = re.compile(r"^\d{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/[a-z0-9._/-]+(:[\w.-]+|@sha256:[0-9a-f]{64})$")
# GCP 는 워커가 태그를 digest 로 바꿀 수단이 없으므로(gcloud 없음) CodeBuild 가 준 digest 만 받는다
AR_IMAGE_RE = re.compile(r"^([a-z0-9-]+)-docker\.pkg\.dev/([a-z][a-z0-9-]{4,28}[a-z0-9])/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
GCP_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
S3_URI_RE = re.compile(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/.+$")


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

    raw_targets = raw.get("targets")
    if raw_targets is None:   # 이전 형식: AWS 하나
        raw_targets = [{"cloud": "aws", **{k: raw[k] for k in ("architecture", "image_uri", "region", "terraform_uri")
                                          if k in raw}}]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise JobError("targets는 클라우드별 배포 대상 목록이어야 합니다")
    targets = [_target(t) for t in raw_targets]
    clouds = [t["cloud"] for t in targets]
    if len(set(clouds)) != len(clouds):
        raise JobError(f"같은 클라우드를 두 번 지정할 수 없습니다: {clouds}")

    ttl = _int(raw.get("ttl_minutes", DEFAULT_TTL_MINUTES), "ttl_minutes")
    ttl = max(5, min(ttl, DEFAULT_TTL_MINUTES))            # 최대 1시간
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "deploy_id": deploy_id,
        "project_id": project_id,
        "container_port": port,
        "size": size,
        "health_path": health_path,
        "env": env,
        "expires_at": expires_at,
        "targets": targets,
    }


def _target(t) -> dict:
    if not isinstance(t, dict):
        raise JobError("target 은 JSON 객체여야 합니다")
    cloud = str(t.get("cloud") or "").lower()
    if cloud not in CLOUD_ARCHITECTURES:
        raise JobError(f"지원하지 않는 클라우드입니다: {cloud or '(없음)'} (가능: {sorted(CLOUD_ARCHITECTURES)})")

    # architecture 는 생략 가능: AgentCore 가 클라우드별 모듈의 아키텍처를 정하므로 워커가 모듈 코드에서 알아낸다
    # (render.resolve_architecture). 넘겨 주면 모듈과 같은지 확인한다
    architecture = str(t.get("architecture") or "").lower() or None
    if architecture and architecture not in CLOUD_ARCHITECTURES[cloud]:
        raise JobError(f"{cloud} 에서 지원하지 않는 아키텍처입니다: {architecture or '(없음)'} "
                       f"(가능: {sorted(CLOUD_ARCHITECTURES[cloud])})")

    image_uri = str(t.get("image_uri") or "")
    if not image_uri:
        raise JobError(f"{cloud} target 에 image_uri 가 없습니다")

    target = {"cloud": cloud, "architecture": architecture, "image_uri": image_uri,
              "terraform_uri": _terraform_uri(t.get("terraform_uri"))}

    if cloud == "aws":
        m = ECR_IMAGE_RE.match(image_uri)
        if not m:
            raise JobError("aws image_uri는 ECR 주소(태그 또는 @sha256 digest 포함)여야 합니다")
        image_region = m.group(1)
        target["region"] = str(t.get("region") or image_region)
        if architecture == "lambda" and target["region"] != image_region:
            # Lambda 는 다른 리전의 ECR 이미지를 쓸 수 없다. apply 에서 늦게 실패하지 않도록 여기서 거른다
            raise JobError(f"Lambda 는 이미지와 같은 리전에 배포해야 합니다 "
                           f"(region={target['region']}, 이미지={image_region})")
    else:
        m = AR_IMAGE_RE.match(image_uri)
        if not m:
            raise JobError("gcp image_uri는 Artifact Registry 주소 + @sha256 digest 여야 합니다 "
                           "(<리전>-docker.pkg.dev/<프로젝트>/<저장소>/<이름>@sha256:...)")
        target["region"] = str(t.get("region") or m.group(1))
        target["gcp_project"] = str(t.get("gcp_project") or m.group(2))
        if not GCP_PROJECT_RE.match(target["gcp_project"]):
            raise JobError(f"GCP 프로젝트 ID 가 올바르지 않습니다: {target['gcp_project']}")
    return target


def _terraform_uri(value) -> str | None:
    """AgentCore 가 저장한 Terraform 모듈 위치. s3://버킷/경로/ 또는 (시험용) 로컬 폴더."""
    if value in (None, ""):
        return None
    value = str(value)
    if value.startswith("s3://"):
        if not S3_URI_RE.match(value):
            raise JobError(f"terraform_uri 형식이 올바르지 않습니다: {value}")
        return value.rstrip("/") + "/"
    if not Path(value).is_dir():
        raise JobError(f"terraform_uri 폴더를 찾을 수 없습니다: {value}")
    return value


def _int(value, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise JobError(f"{key}는 숫자여야 합니다: {value!r}") from None

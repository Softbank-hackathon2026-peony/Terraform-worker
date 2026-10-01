"""1단계: ECR 이미지 확인.

워커는 이미지를 직접 내려받지 않는다(실제 pull은 EC2·Lambda가 한다).
여기서는 이미지가 ECR에 실제로 있는지 확인하고, 태그를 digest(@sha256:...)로 고정한다.
태그가 나중에 다른 이미지를 가리키게 되어도 배포 당시 이미지가 그대로 쓰이게 하기 위해서다.
"""
import re

from . import awscli

IMAGE_RE = re.compile(
    r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9._/-]+)(?::([\w.-]+)|@(sha256:[0-9a-f]{64}))$")


def resolve(image_uri: str) -> str:
    m = IMAGE_RE.match(image_uri)
    if not m:
        raise awscli.AwsError(f"ECR 주소를 해석할 수 없습니다: {image_uri}")
    account, region, repo, tag, digest = m.groups()

    image_id = f"imageDigest={digest}" if digest else f"imageTag={tag}"
    out = awscli.run("ecr", "describe-images", "--registry-id", account,
                     "--repository-name", repo, "--image-ids", image_id, region=region)
    details = out.get("imageDetails") or []
    if not details:
        raise awscli.AwsError(f"ECR에서 이미지를 찾을 수 없습니다: {image_uri}")

    pinned = f"{image_uri.split('/', 1)[0]}/{repo}@{details[0]['imageDigest']}"
    print(f"[image] {image_uri} → {pinned}", flush=True)
    return pinned

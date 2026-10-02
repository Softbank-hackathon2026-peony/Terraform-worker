"""IaC 정적 검사 (파이프라인 21단계 "IaC 검증"): AgentCore 가 만든 Terraform 모듈을 실행 전에 검사한다.

plan 도 실행하기 전에 막아야 하는 것들이다. plan 단계에서 이미 실행되거나(data "external"),
워커의 권한·파일을 사용자 앱으로 흘릴 수 있기 때문이다 (워커는 리소스를 만드는 넓은 권한으로 돈다).
plan 결과는 apply 직전에 policy.py 가 한 번 더 검사한다.

모듈 약속 (AgentCore 와 맞출 것)
  - 폴더 하나에 .tf / .tftpl 파일만. provider·backend 는 쓰지 않는다 (워커가 루트에서 정함)
  - 입력 변수: name, image_uri, container_port, size, env, health_path
  - 출력: endpoint, health_url, resource_id
"""
import re
from pathlib import Path

from . import policy

ALLOWED_SUFFIXES = {".tf", ".tftpl"}
CONTRACT_VARIABLES = ("name", "image_uri", "container_port", "size", "env", "health_path")
CONTRACT_OUTPUTS = ("endpoint", "health_url", "resource_id")
PROVIDER_SOURCES = {"aws": "hashicorp/aws", "gcp": "hashicorp/google"}

# plan·apply 때 임의 코드를 실행하거나, 루트에서 고정한 provider·backend·태그를 우회하는 문법
FORBIDDEN = [
    (r"\bprovisioner\b", "provisioner"),
    (r"\blocal-exec\b|\bremote-exec\b", "local-exec / remote-exec"),
    (r'\bprovider\s+"', "provider 블록 (워커가 루트에서 정함)"),
    (r'\bbackend\s+"|\bcloud\s*\{', "backend / cloud 블록"),
    (r'\bmodule\s+"', "중첩 module (외부 코드 다운로드 가능)"),
    (r"\bdefault_tags\b|\bdefault_labels\b", "default_tags / default_labels (필수 태그를 덮어쓸 수 있음)"),
    (r"\binline_policy\b|\bmanaged_policy_arns\b", "IAM 인라인 정책 (허용한 관리형 정책만 붙일 수 있음)"),
    (r"\baccess_token\b", "access_token (워커의 GCP 토큰을 앱으로 흘릴 수 있음)"),
]
# 읽기 전용이어도 팀의 비밀값을 읽어 앱으로 넘길 수 있으므로 data 소스는 허용 목록으로 제한한다
ALLOWED_DATA = {
    "aws": {"aws_vpc", "aws_subnets", "aws_subnet", "aws_ssm_parameter", "aws_ami", "aws_availability_zones",
            "aws_region", "aws_partition", "aws_iam_policy_document"},
    "gcp": {"google_client_config"},
}
FILE_FUNC_RE = re.compile(r"\b(file\w*|templatefile)\s*\(\s*([^,)]*)")
MODULE_PATH_ARG_RE = re.compile(r'^"\$\{path\.module\}/[^"]+"$')
SSM_BLOCK_RE = re.compile(r'data\s+"aws_ssm_parameter"\s+"[\w-]+"\s*\{(.*?)\n\}', re.DOTALL)


class IacError(ValueError):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("IaC 검사 위반:\n  - " + "\n  - ".join(violations))


def check(module_dir: Path, cloud: str, architecture: str) -> None:
    violations = find_violations(module_dir, cloud, architecture)
    if violations:
        raise IacError(violations)
    print(f"[iac] OK ({cloud}/{architecture})", flush=True)


def find_violations(module_dir: Path, cloud: str, architecture: str) -> list[str]:
    v: list[str] = []
    files = sorted(p for p in module_dir.iterdir()) if module_dir.is_dir() else []
    for p in files:
        if p.is_dir() or p.is_symlink() or p.suffix not in ALLOWED_SUFFIXES:
            v.append(f"허용하지 않는 파일: {p.name} (.tf / .tftpl 만, 하위 폴더 없이)")
    code = "\n".join(p.read_text(encoding="utf-8") for p in files if p.suffix == ".tf" and p.is_file())
    if not code.strip():
        return v + ["Terraform 파일(.tf)이 없습니다"]
    return v + check_code(code, cloud, architecture)


def check_code(code: str, cloud: str, architecture: str) -> list[str]:
    v = [f"금지된 문법: {why}" for pattern, why in FORBIDDEN if re.search(pattern, code)]

    # 앱 권한은 이 모듈에서 만든 리소스를 참조해야 한다. 문자열로 적으면 계정에 이미 있는 역할·계정을 가져다 쓸 수 있다
    for attr, literal in re.findall(r'\b(iam_instance_profile|service_account|role)\s*=\s*"([^"]*)"', code):
        if not (attr == "role" and literal == "roles/run.invoker"):
            v.append(f'{attr} 에 문자열("{literal}")을 직접 쓸 수 없음 — 이 모듈에서 만든 리소스를 참조할 것 '
                     f"(이미 있는 역할·계정 재사용 금지)")

    for src in re.findall(r'\bsource\s*=\s*"([^"]+)"', code):
        if src != PROVIDER_SOURCES[cloud]:
            v.append(f"허용하지 않는 provider source: {src} (가능: {PROVIDER_SOURCES[cloud]})")

    allowed = policy.ALLOWED_TYPES.get(architecture, set())
    for rtype in sorted(set(re.findall(r'\bresource\s+"([A-Za-z0-9_]+)"', code)) - allowed):
        v.append(f"허용하지 않는 리소스 종류: {rtype}")
    for dtype in sorted(set(re.findall(r'\bdata\s+"([A-Za-z0-9_]+)"', code)) - ALLOWED_DATA[cloud]):
        v.append(f"허용하지 않는 data 소스: {dtype}")
    for body in SSM_BLOCK_RE.findall(code):
        if not re.search(r'\bname\s*=\s*"/aws/service/', body):
            v.append("aws_ssm_parameter 는 AWS 공개 파라미터(/aws/service/...)만 읽을 수 있음")

    # 워커 컴퓨터의 다른 파일(자격 증명 등)을 읽지 못하게 모듈 폴더 안의 파일만 허용
    for func, arg in FILE_FUNC_RE.findall(code):
        arg = arg.strip()
        if not MODULE_PATH_ARG_RE.match(arg) or ".." in arg:
            v.append(f'{func}() 는 "${{path.module}}/파일" 형태로 모듈 안의 파일만 읽을 수 있음: {arg or "(없음)"}')

    for name in CONTRACT_VARIABLES:
        if not re.search(rf'\bvariable\s+"{name}"', code):
            v.append(f"입력 변수 누락: {name}")
    for name in CONTRACT_OUTPUTS:
        if not re.search(rf'\boutput\s+"{name}"', code):
            v.append(f"출력 누락: {name}")

    for arn in re.findall(r'\bpolicy_arn\s*=\s*"([^"]+)"', code):
        if arn not in policy.ALLOWED_POLICY_ARNS:
            v.append(f"허용하지 않는 IAM 정책: {arn}")
    if architecture == "ec2" and not re.search(r'\bcpu_credits\s*=\s*"standard"', code):
        v.append('EC2 는 cpu_credits = "standard" 를 유지해야 함 (추가 과금 방지)')
    if architecture == "cloud_run" and not re.search(r"\bdeletion_protection\s*=\s*false\b", code):
        v.append("Cloud Run 은 deletion_protection = false 여야 함 (켜져 있으면 1시간 뒤 destroy 가 실패)")
    return v

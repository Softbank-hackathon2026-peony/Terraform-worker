"""plan 정책 검사: apply 직전에 `terraform show -json tfplan` 결과를 허용 목록과 대조한다.

왜 필요한가
  모듈은 우리가 미리 검증해 두지만, 모듈 코드가 바뀌거나 변수 조합이 예상 밖일 때
  "허용하지 않은 리소스가 만들어지는지", "크기 제한을 넘는지"를 apply 전에 한 번 더 막는다.
  우리 계정에 배포하므로 비용·악용 방지의 마지막 방어선이다.

검사 항목
  - 리소스 종류: 아키텍처별 허용 목록 밖이면 거부
  - 변경 종류: 배포 중에는 delete 가 있으면 안 됨 (살아 있는 다른 리소스를 건드리는 신호)
  - EC2 인스턴스 타입 / Lambda 메모리·타임아웃 상한
  - 보안 그룹 인바운드는 80 번 포트만
  - IAM: 허용한 관리형 정책만, 인라인 정책 금지 (GCP: 서비스 단위 공개 호출 권한만, 프로젝트 IAM 금지)
  - Cloud Run: 메모리 상한, 인스턴스 최대 1개, deletion_protection 꺼짐 (켜져 있으면 destroy 실패)
  - 태그(AWS)·label(GCP)이 있는 리소스에는 pawploy 필수 태그 4개가 모두 있어야 함

AgentCore 가 만든 Terraform 도 같은 검사를 거친다. 실행 전 정적 검사는 iac.py.
"""

ALLOWED_TYPES = {
    "ec2": {
        "aws_security_group",
        "aws_iam_role",
        "aws_iam_role_policy_attachment",
        "aws_iam_instance_profile",
        "aws_instance",
    },
    "lambda": {
        "aws_iam_role",
        "aws_iam_role_policy_attachment",
        "aws_lambda_function",
        "aws_lambda_function_url",
        "aws_lambda_permission",
    },
    "cloud_run": {
        "google_service_account",
        "google_cloud_run_v2_service",
        "google_cloud_run_v2_service_iam_member",
    },
}
# 아키텍처와 무관하게 루트 main.tf 가 만들 수 있는 리소스 (만료 시각 destroy 예약, render.py 참고)
COMMON_TYPES = {"aws_scheduler_schedule"}

ALLOWED_INSTANCE_TYPES = {"t3.micro", "t3.small", "t3.medium"}
MAX_LAMBDA_MEMORY_MB = 2048
MAX_LAMBDA_TIMEOUT_SEC = 60
ALLOWED_INGRESS_PORTS = {80}
# 사용자 앱 역할에 붙일 수 있는 관리형 정책 (CLAUDE.md 5절: 로그 쓰기 + ECR 읽기만)
ALLOWED_POLICY_ARNS = {
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
}
REQUIRED_TAGS = {"pawploy:managed", "pawploy:project_id", "pawploy:deploy_id", "pawploy:expires_at"}
# GCP label 키에는 ":" 를 쓸 수 없어 하이픈으로 바꾼다 (render.py 의 GCP 루트 main.tf 와 같아야 함)
REQUIRED_LABELS = {"pawploy-managed", "pawploy-project-id", "pawploy-deploy-id", "pawploy-expires-at"}
MAX_CLOUD_RUN_MEMORY_MI = 2048
MAX_CLOUD_RUN_INSTANCES = 1
# 공개 접속에 필요한 서비스 단위 권한만 허용
ALLOWED_RUN_IAM = {("roles/run.invoker", "allUsers")}


class PolicyError(ValueError):
    """정책 위반. 메시지에 위반 목록이 줄 단위로 들어간다."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("plan 정책 위반:\n  - " + "\n  - ".join(violations))


def check(plan: dict, architecture: str) -> None:
    """위반이 하나라도 있으면 PolicyError. plan 은 `terraform show -json tfplan` 의 결과."""
    violations = find_violations(plan, architecture)
    if violations:
        raise PolicyError(violations)
    print(f"[policy] OK ({len(plan.get('resource_changes') or [])}개 리소스 변경 검사)", flush=True)


def find_violations(plan: dict, architecture: str) -> list[str]:
    allowed = ALLOWED_TYPES.get(architecture, set()) | COMMON_TYPES
    violations: list[str] = []

    for rc in plan.get("resource_changes") or []:
        if rc.get("mode") == "data":
            continue  # data 소스는 읽기 전용
        actions = set(rc.get("change", {}).get("actions") or [])
        if not actions or actions <= {"no-op", "read"}:
            continue

        address = rc.get("address", "?")
        rtype = rc.get("type", "?")

        if "delete" in actions:
            violations.append(f"{address}: 배포 중 삭제({sorted(actions)})가 계획됨")
        if rtype not in allowed:
            violations.append(f"{address}: 허용하지 않는 리소스 종류 {rtype}")
            continue  # 종류가 틀리면 아래 세부 검사는 의미 없음

        after = rc.get("change", {}).get("after") or {}
        violations += _check_resource(address, rtype, after)

    return violations


def _check_resource(address: str, rtype: str, after: dict) -> list[str]:
    v: list[str] = []

    if rtype == "aws_instance":
        itype = after.get("instance_type")
        if itype not in ALLOWED_INSTANCE_TYPES:
            v.append(f"{address}: 허용하지 않는 인스턴스 타입 {itype} (가능: {sorted(ALLOWED_INSTANCE_TYPES)})")
        # t 계열 기본값 unlimited 는 CPU 를 계속 쓰면 추가 과금된다. 블록을 빼도(빈 목록) 위반
        credits = after.get("credit_specification")
        if credits is not None and not any(c.get("cpu_credits") == "standard" for c in credits):
            v.append(f"{address}: CPU 크레딧은 standard 여야 함 (추가 과금 방지)")

    if rtype == "aws_iam_role_policy_attachment":
        arn = after.get("policy_arn")
        if arn is not None and arn not in ALLOWED_POLICY_ARNS:
            v.append(f"{address}: 허용하지 않는 IAM 정책 {arn}")

    if rtype == "aws_iam_role":
        if any(p.get("policy") for p in after.get("inline_policy") or []):
            v.append(f"{address}: IAM 인라인 정책은 허용하지 않음")
        extra = set(after.get("managed_policy_arns") or []) - ALLOWED_POLICY_ARNS
        if extra:
            v.append(f"{address}: 허용하지 않는 IAM 정책 {sorted(extra)}")

    if rtype == "aws_lambda_function":
        mem = after.get("memory_size")
        if isinstance(mem, (int, float)) and mem > MAX_LAMBDA_MEMORY_MB:
            v.append(f"{address}: Lambda 메모리 {mem}MB 가 상한 {MAX_LAMBDA_MEMORY_MB}MB 초과")
        timeout = after.get("timeout")
        if isinstance(timeout, (int, float)) and timeout > MAX_LAMBDA_TIMEOUT_SEC:
            v.append(f"{address}: Lambda 타임아웃 {timeout}초가 상한 {MAX_LAMBDA_TIMEOUT_SEC}초 초과")

    if rtype == "aws_security_group":
        for rule in after.get("ingress") or []:
            ports = {rule.get("from_port"), rule.get("to_port")}
            if not ports <= ALLOWED_INGRESS_PORTS:
                v.append(f"{address}: 인바운드 포트 {rule.get('from_port')}-{rule.get('to_port')} 는 허용하지 않음 "
                         f"(가능: {sorted(ALLOWED_INGRESS_PORTS)})")

    if rtype == "google_cloud_run_v2_service":
        v += _check_cloud_run(address, after)

    if rtype == "google_cloud_run_v2_service_iam_member":
        if (after.get("role"), after.get("member")) not in ALLOWED_RUN_IAM:
            v.append(f"{address}: 허용하지 않는 Cloud Run 권한 {after.get('role')} → {after.get('member')} "
                     f"(가능: roles/run.invoker → allUsers)")

    # GCP provider default_labels 가 적용된 결과는 terraform_labels 에 들어온다
    labels = after.get("terraform_labels")
    if isinstance(labels, dict):
        missing = REQUIRED_LABELS - set(labels)
        if missing:
            v.append(f"{address}: 필수 label 누락 {sorted(missing)}")

    # provider default_tags 가 적용된 결과는 tags_all 에 들어온다. 값이 plan 시점에 확정되지 않으면
    # (after_unknown) 여기 없을 수 있으므로, 키가 있는 경우에만 검사한다
    tags_all = after.get("tags_all")
    if isinstance(tags_all, dict):
        missing = REQUIRED_TAGS - set(tags_all)
        if missing:
            v.append(f"{address}: 필수 태그 누락 {sorted(missing)}")

    return v


def _check_cloud_run(address: str, after: dict) -> list[str]:
    v: list[str] = []
    if after.get("deletion_protection") is not False:
        v.append(f"{address}: deletion_protection 은 false 여야 함 (켜져 있으면 1시간 뒤 destroy 가 실패)")
    for template in after.get("template") or []:
        for scaling in template.get("scaling") or []:
            max_count = scaling.get("max_instance_count")
            if not isinstance(max_count, int) or not 0 < max_count <= MAX_CLOUD_RUN_INSTANCES:
                v.append(f"{address}: 최대 인스턴스 수 {max_count} 는 허용하지 않음 (1~{MAX_CLOUD_RUN_INSTANCES})")
        if not template.get("scaling"):
            v.append(f"{address}: 최대 인스턴스 수(scaling.max_instance_count)를 정해야 함")
        for container in template.get("containers") or []:
            for res in container.get("resources") or []:
                mem = _memory_mi((res.get("limits") or {}).get("memory"))
                if mem is None or mem > MAX_CLOUD_RUN_MEMORY_MI:
                    v.append(f"{address}: Cloud Run 메모리 {(res.get('limits') or {}).get('memory')} 는 허용하지 않음 "
                             f"(최대 {MAX_CLOUD_RUN_MEMORY_MI}Mi)")
    return v


def _memory_mi(value) -> int | None:
    """Cloud Run 메모리 문자열("512Mi", "1Gi")을 Mi 로. 해석할 수 없으면 None."""
    if not isinstance(value, str):
        return None
    for suffix, factor in (("Gi", 1024), ("Mi", 1), ("G", 1000), ("M", 1)):
        if value.endswith(suffix) and value[:-len(suffix)].isdigit():
            return int(value[:-len(suffix)]) * factor
    return None

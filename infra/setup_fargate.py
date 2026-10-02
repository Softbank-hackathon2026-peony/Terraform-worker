"""Terraform Worker 를 SQS + ECS Fargate 로 올리는 인프라 (팀 AWS 계정, 서울). 이미 있는 것은 건너뛰거나 갱신한다.

  py infra/setup_fargate.py            # 무엇을 할지 출력만 (조회만, 아무것도 만들지 않음)
  py infra/setup_fargate.py --apply    # 실제로 생성 + 워커 이미지 빌드·푸시 + 서비스 배포

구성
  기본 (infra/setup-aws.sh)  S3 버킷 · DynamoDB 테이블 · 만료 큐 pawploy-destroy · Scheduler 역할 ppw-scheduler
  SQS FIFO                  pawploy-jobs.fifo (배포·삭제 요청) + pawploy-jobs-dlq.fifo (3회 실패 시)
  ECR                       pawploy-tf-worker (푸시 때 스캔, 최근 10개만 보관)
  Secrets Manager           pawploy/gcp-worker-key (GCP 서비스 계정 키 → 컨테이너 환경변수 GOOGLE_CREDENTIALS)
  CloudWatch Logs           /ecs/pawploy-tf-worker (7일 보관)
  IAM                       ppw-ecs-execution (이미지·로그·비밀값) / ppw-worker-task (워커 권한, pawploy-* 리소스로 제한)
  ECS                       클러스터 pawploy · 작업 정의 pawploy-tf-worker (0.5 vCPU / 2GB) · 서비스 1개
  네트워크                   기본 VPC 공개 서브넷 + 공인 IP (NAT 비용 없음), 보안 그룹 ppw-worker (들어오는 연결 없음)

비용(서울, 대략): Fargate 상시 1개 월 약 $24, Secrets Manager $0.4, 나머지는 시험 규모에서 거의 0.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGION = os.environ.get("PAWPLOY_REGION", "ap-northeast-2")
APPLY = "--apply" in sys.argv
GCP_KEY_FILE = os.environ.get("GCP_KEY_FILE", r"C:\keys\pawploy-worker.json")

CLUSTER, SERVICE, FAMILY, CONTAINER = "pawploy", "pawploy-tf-worker", "pawploy-tf-worker", "worker"
ECR_REPO, LOG_GROUP, SECRET_NAME = "pawploy-tf-worker", "/ecs/pawploy-tf-worker", "pawploy/gcp-worker-key"
JOBS_QUEUE, DLQ = "pawploy-jobs.fifo", "pawploy-jobs-dlq.fifo"
EXEC_ROLE, TASK_ROLE, SG_NAME = "ppw-ecs-execution", "ppw-worker-task", "ppw-worker"
TABLE, DESTROY_QUEUE, SCHED_ROLE = "pawploy-deployments", "pawploy-destroy", "ppw-scheduler"
ALLOWED_APP_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
]


def aws(*args, check=True, as_json=True):
    cmd = ["aws", *args, "--region", REGION] + (["--output", "json"] if as_json else [])
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        if check:
            sys.exit(f"[실패] aws {' '.join(args[:2])}\n{p.stderr.strip()}")
        return None
    return json.loads(p.stdout) if as_json and p.stdout.strip() else p.stdout


def change(desc, *args, **kw):
    print(f"  + {desc}")
    return aws(*args, **kw) if APPLY else None


def tmpjson(data) -> str:
    """긴 JSON 인자는 file:// 로 넘긴다 (Windows 명령줄 길이·따옴표 문제 회피)."""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, f)
    f.close()
    return "file://" + f.name


def _bash() -> str:
    """Windows 의 PATH 에 있는 bash 는 WSL 일 수 있다 → Git Bash 를 먼저 찾는다."""
    if os.environ.get("PAWPLOY_BASH"):
        return os.environ["PAWPLOY_BASH"]
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            git_bash = Path(base) / "Git" / "bin" / "bash.exe"
            if git_bash.exists():
                return str(git_bash)
    return "bash"


def run(cmd, **kw):
    print("  $ " + " ".join(cmd))
    if APPLY:
        subprocess.run(cmd, check=True, **kw)


ACCOUNT = aws("sts", "get-caller-identity")["Account"]
BUCKET = os.environ.get("PAWPLOY_BUCKET", f"pawploy-tf-{ACCOUNT}-{REGION}")
ARN = f"arn:aws:%s:{REGION}:{ACCOUNT}:%s"
QUEUE_URL = lambda name: f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{name}"   # noqa: E731


def worker_task_policy(secret_arn: str | None) -> dict:
    """워커 권한: 필요한 서비스만, 사용자 앱 리소스는 pawploy-* 이름으로, 플랫폼 리소스는 정확한 ARN 으로 제한."""
    role = f"arn:aws:iam::{ACCOUNT}:role/pawploy-*"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Ec2App", "Effect": "Allow", "Resource": "*", "Action": [
            "ec2:Describe*", "ec2:Get*", "ec2:RunInstances", "ec2:TerminateInstances", "ec2:CreateTags",
            "ec2:DeleteTags", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
            "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
            "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:ModifyInstanceAttribute",
            "ec2:ModifyInstanceCreditSpecification", "ec2:ModifyInstanceMetadataOptions"]},
        {"Sid": "PublicAmi", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
         "Resource": f"arn:aws:ssm:{REGION}::parameter/aws/service/*"},
        {"Sid": "AppRoles", "Effect": "Allow", "Resource": role, "Action": [
            "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
            "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole"]},
        {"Sid": "AppRolePolicies", "Effect": "Allow", "Resource": role,
         "Action": ["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
         "Condition": {"ArnEquals": {"iam:PolicyARN": ALLOWED_APP_POLICIES}}},
        {"Sid": "AppInstanceProfiles", "Effect": "Allow",
         "Resource": f"arn:aws:iam::{ACCOUNT}:instance-profile/pawploy-*", "Action": [
            "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
            "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile"]},
        {"Sid": "PassAppRoles", "Effect": "Allow", "Action": "iam:PassRole", "Resource": role,
         "Condition": {"StringEquals": {"iam:PassedToService": ["ec2.amazonaws.com", "lambda.amazonaws.com"]}}},
        {"Sid": "PassSchedulerRole", "Effect": "Allow", "Action": "iam:PassRole",
         "Resource": f"arn:aws:iam::{ACCOUNT}:role/{SCHED_ROLE}",
         "Condition": {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}},
        {"Sid": "LambdaApp", "Effect": "Allow", "Action": "lambda:*",
         "Resource": ARN % ("lambda", "function:pawploy-*")},
        {"Sid": "EcrRead", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "EcrImages", "Effect": "Allow", "Resource": ARN % ("ecr", "repository/*"), "Action": [
            "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
            "ecr:GetRepositoryPolicy", "ecr:SetRepositoryPolicy"]},   # Lambda 가 이미지 접근 정책을 붙일 때
        {"Sid": "Schedules", "Effect": "Allow", "Resource": ARN % ("scheduler", "schedule/default/pawploy-*"),
         "Action": ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule"]},
        {"Sid": "AppLogs", "Effect": "Allow", "Resource": "*",
         "Action": ["logs:FilterLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]},
        {"Sid": "StateBucket", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{BUCKET}"},
        {"Sid": "StateObjects", "Effect": "Allow", "Resource": f"arn:aws:s3:::{BUCKET}/*",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]},
        {"Sid": "StatusTable", "Effect": "Allow", "Resource": ARN % ("dynamodb", f"table/{TABLE}"),
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Scan"]},
        {"Sid": "Queues", "Effect": "Allow",
         "Resource": [ARN % ("sqs", JOBS_QUEUE), ARN % ("sqs", DESTROY_QUEUE)],
         "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueAttributes"]},
        {"Sid": "TaskProtection", "Effect": "Allow", "Action": "ecs:UpdateTaskProtection",
         "Resource": ARN % ("ecs", f"task/{CLUSTER}/*")},
        {"Sid": "Misc", "Effect": "Allow", "Resource": "*",
         "Action": ["tag:GetResources", "sts:GetCallerIdentity"]},
    ]}


def ecs_trust() -> dict:
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}}}]}


def main() -> None:
    print(f"계정 {ACCOUNT} / 리전 {REGION} / {'실제 생성' if APPLY else '출력만 (--apply 로 실행)'}")

    print("[0] 기본 인프라 (infra/setup-aws.sh)")
    subprocess.run([_bash(), str(ROOT / "infra" / "setup-aws.sh"), *(["--apply"] if APPLY else [])], check=True,
                   stdout=subprocess.DEVNULL if APPLY else None)

    print(f"[1] SQS FIFO {JOBS_QUEUE} + {DLQ}")
    if aws("sqs", "get-queue-url", "--queue-name", JOBS_QUEUE, check=False):
        print("  이미 있음")
    else:
        change(f"DLQ {DLQ}", "sqs", "create-queue", "--queue-name", DLQ, "--attributes", tmpjson(
            {"FifoQueue": "true", "MessageRetentionPeriod": "1209600", "SqsManagedSseEnabled": "true"}))
        change(f"작업 큐 {JOBS_QUEUE} (가시성 900초, 3회 실패 시 DLQ)", "sqs", "create-queue", "--queue-name", JOBS_QUEUE,
               "--attributes", tmpjson({
                   "FifoQueue": "true", "ContentBasedDeduplication": "true", "VisibilityTimeout": "900",
                   "SqsManagedSseEnabled": "true",
                   "RedrivePolicy": json.dumps({"deadLetterTargetArn": ARN % ("sqs", DLQ), "maxReceiveCount": "3"})}))

    print(f"[2] ECR {ECR_REPO}")
    if aws("ecr", "describe-repositories", "--repository-names", ECR_REPO, check=False):
        print("  이미 있음")
    else:
        change("저장소 생성 (푸시 때 스캔)", "ecr", "create-repository", "--repository-name", ECR_REPO,
               "--image-scanning-configuration", "scanOnPush=true", "--tags", "Key=pawploy:platform,Value=true")
        change("최근 10개만 보관", "ecr", "put-lifecycle-policy", "--repository-name", ECR_REPO,
               "--lifecycle-policy-text", tmpjson({"rules": [{"rulePriority": 1, "description": "keep last 10",
                   "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 10},
                   "action": {"type": "expire"}}]}))

    print(f"[3] Secrets Manager {SECRET_NAME}")
    secret = aws("secretsmanager", "describe-secret", "--secret-id", SECRET_NAME, check=False)
    secret_arn = secret["ARN"] if secret else None
    if secret:
        print("  이미 있음")
    elif Path(GCP_KEY_FILE).exists():
        out = change(f"GCP 키 {GCP_KEY_FILE} 저장", "secretsmanager", "create-secret", "--name", SECRET_NAME,
                     "--secret-string", "file://" + GCP_KEY_FILE.replace("\\", "/"),
                     "--tags", "Key=pawploy:platform,Value=true")
        secret_arn = out["ARN"] if out else None
    else:
        print(f"  ⚠️ {GCP_KEY_FILE} 없음 → GCP 배포는 동작하지 않음 (GCP_KEY_FILE 로 경로 지정)")

    print(f"[4] CloudWatch Logs {LOG_GROUP}")
    groups = aws("logs", "describe-log-groups", "--log-group-name-prefix", LOG_GROUP)["logGroups"]
    if any(g["logGroupName"] == LOG_GROUP for g in groups):
        print("  이미 있음")
    else:
        change("로그 그룹 생성", "logs", "create-log-group", "--log-group-name", LOG_GROUP, as_json=False)
        change("7일 보관", "logs", "put-retention-policy", "--log-group-name", LOG_GROUP,
               "--retention-in-days", "7", as_json=False)

    print(f"[5] IAM {EXEC_ROLE} / {TASK_ROLE}")
    for name in (EXEC_ROLE, TASK_ROLE):
        if aws("iam", "get-role", "--role-name", name, check=False):
            print(f"  {name} 이미 있음 (정책은 갱신)")
        else:
            change(f"역할 {name}", "iam", "create-role", "--role-name", name, "--assume-role-policy-document",
                   tmpjson(ecs_trust()), "--tags", "Key=pawploy:platform,Value=true")
    change("실행 역할: 이미지 pull·로그", "iam", "attach-role-policy", "--role-name", EXEC_ROLE, "--policy-arn",
           "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy", as_json=False)
    if secret_arn or not APPLY:
        change("실행 역할: GCP 키 비밀값 읽기", "iam", "put-role-policy", "--role-name", EXEC_ROLE, "--policy-name",
               "read-gcp-key", "--policy-document", tmpjson({"Version": "2012-10-17", "Statement": [{
                   "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                   "Resource": secret_arn or "<비밀값 ARN>"}]}), as_json=False)
    change("작업 역할: 워커 권한 (pawploy-* 로 제한)", "iam", "put-role-policy", "--role-name", TASK_ROLE,
           "--policy-name", "worker", "--policy-document", tmpjson(worker_task_policy(secret_arn)), as_json=False)

    print(f"[6] ECS 클러스터 {CLUSTER}")
    clusters = aws("ecs", "describe-clusters", "--clusters", CLUSTER)["clusters"]
    if any(c["status"] == "ACTIVE" for c in clusters):
        print("  이미 있음")
    else:
        change("클러스터 생성", "ecs", "create-cluster", "--cluster-name", CLUSTER,
               "--tags", "key=pawploy:platform,value=true")

    print(f"[7] 네트워크: 기본 VPC + 보안 그룹 {SG_NAME}")
    vpc = aws("ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true")["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in aws("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc}",
                                          "Name=default-for-az,Values=true")["Subnets"]]
    sgs = aws("ec2", "describe-security-groups", "--filters", f"Name=vpc-id,Values={vpc}",
              f"Name=group-name,Values={SG_NAME}")["SecurityGroups"]
    sg = sgs[0]["GroupId"] if sgs else None
    if sg:
        print(f"  이미 있음: {sg}")
    else:
        out = change("보안 그룹 생성 (들어오는 연결 없음, 나가는 연결만)", "ec2", "create-security-group",
                     "--group-name", SG_NAME, "--description", "Pawploy terraform worker (egress only)",
                     "--vpc-id", vpc)
        sg = out["GroupId"] if out else "<보안 그룹>"
    print(f"  VPC {vpc}, 서브넷 {len(subnets)}개")

    print(f"[8] 워커 이미지 빌드·푸시 → {ECR_REPO}")
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    tag = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                         text=True).stdout.strip() or "dev"
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        tag += "-dirty"
    image = f"{registry}/{ECR_REPO}:{tag}"
    run(["docker", "build", "--platform", "linux/amd64", "--provenance=false", "-t", image, str(ROOT)])
    if APPLY:
        password = aws("ecr", "get-login-password", as_json=False)
        subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", registry],
                       input=password, text=True, check=True, capture_output=True)
    run(["docker", "push", image], stdout=subprocess.DEVNULL)
    digest = None
    if APPLY:
        digest = aws("ecr", "describe-images", "--repository-name", ECR_REPO,
                     "--image-ids", f"imageTag={tag}")["imageDetails"][0]["imageDigest"]
    image_ref = f"{registry}/{ECR_REPO}@{digest}" if digest else image

    print(f"[9] 작업 정의 {FAMILY} (0.5 vCPU / 2GB, consume)")
    env = {
        "PAWPLOY_REGION": REGION, "PAWPLOY_STATE_BUCKET": BUCKET, "PAWPLOY_ARTIFACT_BUCKET": BUCKET,
        "PAWPLOY_STATUS_TABLE": TABLE, "PAWPLOY_JOBS_QUEUE_URL": QUEUE_URL(JOBS_QUEUE),
        "PAWPLOY_DESTROY_QUEUE_ARN": ARN % ("sqs", DESTROY_QUEUE), "PAWPLOY_DESTROY_QUEUE_URL": QUEUE_URL(DESTROY_QUEUE),
        "PAWPLOY_SCHEDULER_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/{SCHED_ROLE}",
    }
    container = {
        "name": CONTAINER, "image": image_ref, "essential": True, "command": ["consume"], "stopTimeout": 120,
        "environment": [{"name": k, "value": v} for k, v in env.items()],
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": LOG_GROUP, "awslogs-region": REGION, "awslogs-stream-prefix": "worker"}},
    }
    if secret_arn:
        container["secrets"] = [{"name": "GOOGLE_CREDENTIALS", "valueFrom": secret_arn}]
    taskdef = change("작업 정의 등록", "ecs", "register-task-definition", "--cli-input-json", tmpjson({
        "family": FAMILY, "requiresCompatibilities": ["FARGATE"], "networkMode": "awsvpc",
        "cpu": "512", "memory": "2048", "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64"},
        "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{EXEC_ROLE}",
        "taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{TASK_ROLE}",
        "containerDefinitions": [container]}))
    taskdef_arn = taskdef["taskDefinition"]["taskDefinitionArn"] if taskdef else f"{FAMILY}:<새 리비전>"

    print(f"[10] ECS 서비스 {SERVICE} (작업 1개)")
    services = aws("ecs", "describe-services", "--cluster", CLUSTER, "--services", SERVICE, check=False)
    active = services and any(s["status"] == "ACTIVE" for s in services.get("services", []))
    if active:
        change("새 작업 정의로 갱신", "ecs", "update-service", "--cluster", CLUSTER, "--service", SERVICE,
               "--task-definition", taskdef_arn, "--force-new-deployment")
    else:
        change("서비스 생성 (공개 서브넷 + 공인 IP)", "ecs", "create-service", "--cluster", CLUSTER,
               "--service-name", SERVICE, "--task-definition", taskdef_arn, "--desired-count", "1",
               "--launch-type", "FARGATE",
               "--deployment-configuration", "minimumHealthyPercent=100,maximumPercent=200",
               "--network-configuration", "awsvpcConfiguration={subnets=[%s],securityGroups=[%s],assignPublicIp=ENABLED}"
               % (",".join(subnets), sg), "--tags", "key=pawploy:platform,value=true")

    print(f"""
Main Server 가 쓸 값
  작업 큐    {QUEUE_URL(JOBS_QUEUE)}   (FIFO, MessageGroupId = deploy_id)
  작업 JSON  s3://{BUCKET}/jobs/<deploy_id>/<요청 ID>.json
  결과       DynamoDB {TABLE} (키 deploy_id)
  로그       CloudWatch Logs {LOG_GROUP}""")


if __name__ == "__main__":
    main()

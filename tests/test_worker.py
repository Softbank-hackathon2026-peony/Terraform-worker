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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_TF = ROOT / "tools" / "fake-terraform.py"
FAKE_AWS = ROOT / "tools" / "fake-aws.py"


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
        self.base_env.pop("FAKE_TF_FAIL", None)
        self.base_env.pop("PAWPLOY_STATE_BUCKET", None)
        self.base_env.pop("PAWPLOY_ARTIFACT_BUCKET", None)

    # ---------- 도우미 ----------

    def run_worker(self, *args, **env):
        p = subprocess.run([sys.executable, "-m", "tfworker", *args], cwd=ROOT,
                           env={**self.base_env, **env}, capture_output=True, text=True, encoding="utf-8")
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

    def result(self, deploy_id="dep-test-ec2") -> dict:
        return json.loads((self.work / deploy_id / "result.json").read_text(encoding="utf-8"))

    def aws_env(self, **extra) -> dict:
        """가짜 aws CLI 를 쓰는 환경 (PAWPLOY_OFFLINE 을 꺼서 AWS 호출 단계가 실제로 돌게 함)."""
        self.aws_log = self.tmp / "aws.jsonl"
        return {"PAWPLOY_OFFLINE": "", "AWS_BIN": _fake_bin(self.tmp, FAKE_AWS, "aws"),
                "FAKE_AWS_LOG": str(self.aws_log), **extra}

    def aws_calls(self, service: str, op: str) -> list[list[str]]:
        if not self.aws_log.exists():
            return []
        calls = [json.loads(line) for line in self.aws_log.read_text(encoding="utf-8").splitlines()]
        return [c for c in calls if c[:2] == [service, op]]

    # ---------- 시나리오 ----------

    def test_deploy_then_destroy_ok(self):
        code, out = self.run_worker("deploy", self.write_job())
        self.assertEqual(code, 0, out)
        r = self.result()
        self.assertEqual(r["status"], "running")
        self.assertEqual(r["endpoint"], self.healthy_endpoint)
        self.assertEqual(r["resource_id"], "i-0fake000000000000")

        wd = self.work / "dep-test-ec2"
        self.assertTrue((wd / "main.tf").exists())
        self.assertTrue((wd / "modules" / "ec2" / "main.tf").exists(), "모듈이 작업 폴더에 복사되어야 함")
        tfvars = json.loads((wd / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["env"], {"APP_MODE": "test"})
        self.assertEqual(tfvars["region"], "ap-northeast-2", "region 은 이미지 주소에서 추출")
        self.assertIn('source         = "./modules/ec2"', (wd / "main.tf").read_text(encoding="utf-8"))

        code, out = self.run_worker("destroy", "dep-test-ec2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result()["status"], "destroyed")

    def test_apply_failure_cleans_up_and_diagnoses(self):
        code, out = self.run_worker("deploy", self.write_job(), FAKE_TF_FAIL="apply")
        self.assertEqual(code, 1, out)
        r = self.result()
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r.get("destroyed"), "apply 실패 후 자동 destroy 가 돌아야 함")
        self.assertIn("InvalidParameterValue", r.get("log_tail", ""))
        self.assertTrue((self.work / "dep-test-ec2" / "diagnosis_input.json").exists())

    def test_unhealthy_returns_3(self):
        code, out = self.run_worker("deploy", self.write_job(), FAKE_TF_ENDPOINT="http://127.0.0.1:9")
        self.assertEqual(code, 3, out)
        r = self.result()
        self.assertEqual(r["status"], "unhealthy")
        self.assertEqual(r["resource_id"], "i-0fake000000000000", "리소스는 남아 있어야 함 (진단 대상)")
        self.assertTrue((self.work / "dep-test-ec2" / "diagnosis_input.json").exists())

    def test_init_failure_leaves_no_resources_and_id_reusable(self):
        # init 에서 실패하면 만들어진 리소스가 없다. destroy 를 돌리지 않아야 하고(돌리면 destroy_failed 가 됨),
        # 같은 deploy_id 로 바로 다시 배포할 수 있어야 한다
        job = self.write_job()
        code, out = self.run_worker("deploy", job, FAKE_TF_FAIL="init")
        self.assertEqual(code, 1, out)
        r = self.result()
        self.assertEqual(r["status"], "failed")
        self.assertNotIn("destroyed", r, "apply 전 실패에는 destroy 가 돌면 안 됨")

        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.result()["status"], "running")

    def test_policy_violation_blocks_apply(self):
        # plan 에 허용 밖 인스턴스 타입 / 리소스 종류가 있으면 apply 전에 failed 로 끝나야 하고, destroy 는 돌지 않는다
        cases = {
            "instance_type": dict(FAKE_TF_INSTANCE_TYPE="t3.xlarge"),
            "resource_type": dict(FAKE_TF_EXTRA_RESOURCE="aws_s3_bucket"),
        }
        for name, env in cases.items():
            with self.subTest(case=name):
                code, out = self.run_worker("deploy", self.write_job(deploy_id=f"dep-pol-{name.replace('_', '-')}"), **env)
                self.assertEqual(code, 1, out)
                r = self.result(f"dep-pol-{name.replace('_', '-')}")
                self.assertEqual(r["status"], "failed")
                self.assertIn("plan 정책 위반", r["error"])
                self.assertNotIn("destroyed", r)
                self.assertNotIn("Apply complete", out, "정책 위반이면 apply 가 실행되면 안 됨")

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

        v = policy.find_violations(plan(
            change("aws_instance", {"instance_type": "m5.large", "tags_all": {"pawploy:managed": "true"}}),
            change("aws_lambda_function", {"memory_size": 4096, "timeout": 900}),   # ec2 배포에 Lambda 는 허용 밖
            change("aws_security_group", {"ingress": [{"from_port": 22, "to_port": 22}], **ok_tags}),
            change("aws_iam_role", actions=("delete",)),
        ), "ec2")
        joined = "\n".join(v)
        self.assertIn("인스턴스 타입 m5.large", joined)
        self.assertIn("필수 태그 누락", joined)
        self.assertIn("허용하지 않는 리소스 종류 aws_lambda_function", joined)
        self.assertIn("인바운드 포트 22-22", joined)
        self.assertIn("삭제", joined)

        v = policy.find_violations(plan(change("aws_lambda_function", {"memory_size": 4096, "timeout": 900, **ok_tags})), "lambda")
        self.assertEqual(len(v), 2, v)

    def test_input_errors_return_2(self):
        cases = {
            "size": dict(size="xlarge"),
            "architecture": dict(architecture="ecs_fargate"),
            "image_uri": dict(image_uri="docker.io/library/nginx:latest"),
            "deploy_id": dict(deploy_id="Bad_ID"),
            "env": dict(env={"1BAD": "x"}),
            "lambda_region": dict(architecture="lambda", region="us-east-1"),   # 이미지는 ap-northeast-2
        }
        for name, bad in cases.items():
            with self.subTest(field=name):
                code, out = self.run_worker("deploy", self.write_job(f"bad-{name}.json", **bad))
                self.assertEqual(code, 2, out)
                self.assertIn("입력 오류", out)

    def test_redeploy_same_id_is_rejected_while_running(self):
        job = self.write_job()
        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)

        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 2, out)
        self.assertIn("이미 사용 중인 deploy_id", out)
        self.assertEqual(self.result()["status"], "running", "기존 결과가 덮어써지면 안 됨")

        # destroy 뒤에는 같은 id 재사용 가능
        self.assertEqual(self.run_worker("destroy", "dep-test-ec2")[0], 0)
        code, out = self.run_worker("deploy", job)
        self.assertEqual(code, 0, out)

    def test_destroy_without_state_is_refused(self):
        # apply 실패 → 자동 정리까지 끝난 뒤에는 state 가 없다. 그 상태에서 destroy 를 또 부르면 거부해야 함
        self.run_worker("deploy", self.write_job(), FAKE_TF_FAIL="apply")
        (self.work / "dep-test-ec2" / "terraform.tfstate").unlink(missing_ok=True)

        code, out = self.run_worker("destroy", "dep-test-ec2")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.result()["status"], "destroy_failed")
        self.assertIn("state", self.result()["error"])

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
            "deploy_id": "dep-test-rec", "project_id": "prj_test",
            "image_uri": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/pawploy-sample:latest",
            "recommendation_uri": str(rec),
            "env": {"APP_MODE": "job"},
        }), encoding="utf-8")

        code, out = self.run_worker("deploy", str(job_path))
        self.assertEqual(code, 0, out)
        tfvars = json.loads((self.work / "dep-test-rec" / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["container_port"], 3000)
        self.assertEqual(tfvars["size"], "micro")
        self.assertEqual(tfvars["health_path"], "/health")
        self.assertEqual(tfvars["env"], {"FROM_REC": "1", "APP_MODE": "job"}, "작업 입력 env 가 추천 env 를 덮어씀")
        self.assertEqual(self.result("dep-test-rec")["architecture"], "lambda")
        self.assertTrue((self.work / "dep-test-rec" / "modules" / "lambda" / "main.tf").exists())

    def test_image_is_pinned_to_digest_and_status_goes_to_dynamodb(self):
        env = self.aws_env(PAWPLOY_STATUS_TABLE="pawploy-deployments", PAWPLOY_ARTIFACT_BUCKET="bkt")
        code, out = self.run_worker("deploy", self.write_job(), **env)
        self.assertEqual(code, 0, out)

        # ① 태그 → digest 고정
        tfvars = json.loads((self.work / "dep-test-ec2" / "terraform.tfvars.json").read_text(encoding="utf-8"))
        self.assertEqual(tfvars["image_uri"],
                         "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/pawploy-sample@sha256:" + "a" * 64)
        self.assertEqual(len(self.aws_calls("ecr", "describe-images")), 1)

        # ④ S3 보관은 state·결과 파일을 제외하고 올린다
        sync = self.aws_calls("s3", "sync")
        self.assertEqual(len(sync), 1, "init 뒤 한 번만 보관")
        self.assertIn("s3://bkt/workdirs/dep-test-ec2/", sync[0])
        for excluded in ("*.tfstate", "result.json", "plan.json"):
            self.assertIn(excluded, sync[0])

        # DynamoDB 에는 상태가 바뀔 때마다 기록되고 마지막은 running
        puts = self.aws_calls("dynamodb", "put-item")
        statuses = [json.loads(c[c.index("--item") + 1])["status"]["S"] for c in puts]
        self.assertEqual(statuses[0], "preparing")
        self.assertEqual(statuses[-1], "running")
        last = json.loads(puts[-1][puts[-1].index("--item") + 1])
        self.assertEqual(last["deploy_id"], {"S": "dep-test-ec2"})
        self.assertEqual(last["project_id"], {"S": "prj_test"})
        self.assertEqual(last["endpoint"], {"S": self.healthy_endpoint})
        self.assertIn("--region", puts[-1])

    def test_missing_image_returns_2(self):
        code, out = self.run_worker("deploy", self.write_job(), **self.aws_env(FAKE_AWS_ECR_MISSING="1"))
        self.assertEqual(code, 2, out)
        self.assertEqual(self.result()["status"], "failed")
        self.assertFalse((self.work / "dep-test-ec2" / "main.tf").exists(), "이미지가 없으면 Terraform 생성 전에 멈춤")

    def test_dynamodb_attr_roundtrip(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import store
        value = {"s": "x", "n": 3, "f": 1.5, "b": True, "none": None, "l": [1, "a"], "m": {"k": "v"}}
        self.assertEqual(store.from_attr(store.to_attr(value)), value)
        self.assertEqual(store.to_attr(True), {"BOOL": True}, "bool 이 숫자로 저장되면 안 됨")

    def test_s3_backend_args_include_lockfile(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import render
        args = render.backend_args({"state_bucket": "b", "project_id": "p", "deploy_id": "d", "region": "ap-northeast-2"})
        self.assertIn("-backend-config=use_lockfile=true", args)
        self.assertIn("-backend-config=key=deployments/p/d.tfstate", args)
        self.assertEqual(render.backend_args({"state_bucket": None}), [])


if __name__ == "__main__":
    unittest.main()

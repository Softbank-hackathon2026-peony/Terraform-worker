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


def _fake_terraform_bin(tmp: Path) -> str:
    """shutil.which 가 찾을 수 있는 실행 파일 경로를 돌려준다 (Windows 는 .cmd 래퍼)."""
    if os.name == "nt":
        wrapper = tmp / "terraform.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{FAKE_TF}" %*\r\n', encoding="utf-8")
        return str(wrapper)
    return str(FAKE_TF)


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
            "TERRAFORM_BIN": _fake_terraform_bin(self.tmp),
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

    def test_s3_backend_args_include_lockfile(self):
        sys.path.insert(0, str(ROOT))
        from tfworker import render
        args = render.backend_args({"state_bucket": "b", "project_id": "p", "deploy_id": "d", "region": "ap-northeast-2"})
        self.assertIn("-backend-config=use_lockfile=true", args)
        self.assertIn("-backend-config=key=deployments/p/d.tfstate", args)
        self.assertEqual(render.backend_args({"state_bucket": None}), [])


if __name__ == "__main__":
    unittest.main()

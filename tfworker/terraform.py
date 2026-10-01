"""terraform CLI 실행 도우미."""
import json
import os
import shutil
import subprocess
from pathlib import Path

TERRAFORM = os.environ.get("TERRAFORM_BIN", "terraform")


class TerraformError(RuntimeError):
    def __init__(self, args, code, output, message=None):
        super().__init__(message or f"terraform {' '.join(args)} 실패 (exit {code})")
        self.output = output


def run(workdir: Path, *args: str, capture_json: bool = False):
    if shutil.which(TERRAFORM) is None:
        raise TerraformError(args, 127, "", message="terraform 실행 파일을 찾을 수 없습니다. "
                             "설치 후 PATH에 추가하거나 TERRAFORM_BIN을 지정하세요")

    cmd = [TERRAFORM, f"-chdir={workdir}", *args]
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
    print(f"[tf] $ terraform {' '.join(args)}", flush=True)

    if capture_json:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            raise TerraformError(args, p.returncode, p.stderr)
        try:
            return json.loads(p.stdout or "{}")
        except ValueError:
            raise TerraformError(args, 0, p.stdout, message="terraform 출력(JSON)을 해석할 수 없습니다") from None

    # 로그를 실시간으로 보여주면서 마지막 부분은 에러 메시지용으로 보관
    lines = []
    with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as p:
        for line in p.stdout:
            print(line, end="", flush=True)
            lines.append(line)
            lines = lines[-200:]
    if p.returncode != 0:
        raise TerraformError(args, p.returncode, "".join(lines))

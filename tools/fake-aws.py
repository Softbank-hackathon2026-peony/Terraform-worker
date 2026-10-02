#!/usr/bin/env python3
"""가짜 aws CLI: 호출 내용을 기록하고 정해진 응답을 돌려준다 (AWS 없이 워커를 시험할 때).

사용법 (PAWPLOY_OFFLINE 은 끄고):
  AWS_BIN=tools/fake-aws.py FAKE_AWS_LOG=/tmp/aws.jsonl python -m tfworker deploy examples/job-ec2.json

흉내 내는 명령
  ecr describe-images                      → 이미지 1건 (digest sha256:aaaa…). FAKE_AWS_ECR_MISSING=1 이면 빈 결과
  dynamodb put-item                        → {}
  dynamodb scan                            → FAKE_AWS_SCAN_ITEMS (JSON 배열) 를 Items 로
  s3 sync / s3 cp                          → 빈 출력 (cp 대상이 "-" 면 FAKE_AWS_S3_BODY)
  resourcegroupstaggingapi get-resources   → FAKE_AWS_TAGGED (JSON 배열) 를 ResourceTagMappingList 로
  ec2 get-console-output / logs tail       → 가짜 로그 텍스트
  bedrock-agentcore invoke-agent-runtime   → 출력 파일에 {"summary": ...} 저장
  sts get-caller-identity                  → 계정 123456789012

환경변수
  FAKE_AWS_LOG   호출 argv 를 한 줄에 하나씩 JSON 으로 덧붙여 기록할 파일 (테스트가 검증에 씀)
  FAKE_AWS_FAIL  "ecr describe-images" 처럼 "<서비스> <명령>" 이 일치하면 exit 1
"""
import json
import os
import sys
from pathlib import Path

DIGEST = "sha256:" + "a" * 64


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_AWS_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv, ensure_ascii=False) + "\n")

    service, op = (argv + ["", ""])[:2]
    if os.environ.get("FAKE_AWS_FAIL") == f"{service} {op}":
        print(f"An error occurred (Fake) when calling the {op} operation: FAKE_AWS_FAIL", file=sys.stderr)
        return 1

    def arg(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    if (service, op) == ("ecr", "describe-images"):
        if os.environ.get("FAKE_AWS_ECR_MISSING") == "1":
            print("An error occurred (ImageNotFoundException) when calling the DescribeImages operation (fake)",
                  file=sys.stderr)
            return 254
        print(json.dumps({"imageDetails": [{"imageDigest": DIGEST, "imageTags": ["latest"],
                                            "repositoryName": arg("--repository-name")}]}))
    elif (service, op) == ("dynamodb", "put-item"):
        print("{}")
    elif (service, op) == ("dynamodb", "scan"):
        print(json.dumps({"Items": json.loads(os.environ.get("FAKE_AWS_SCAN_ITEMS", "[]")), "Count": 0}))
    elif service == "s3":
        if op == "cp" and len(argv) > 3 and argv[3] == "-":
            print(os.environ.get("FAKE_AWS_S3_BODY", "{}"))
    elif (service, op) == ("resourcegroupstaggingapi", "get-resources"):
        print(json.dumps({"ResourceTagMappingList": json.loads(os.environ.get("FAKE_AWS_TAGGED", "[]"))}))
    elif (service, op) == ("ec2", "get-console-output"):
        print(json.dumps({"InstanceId": arg("--instance-id"), "Output": "[pawploy] fake console output"}))
    elif (service, op) == ("logs", "tail"):
        print("fake lambda log line")
    elif (service, op) == ("bedrock-agentcore", "invoke-agent-runtime"):
        # 출력 파일은 위치 인자 (awscli.run 이 --region 을 뒤에 붙이므로 이름으로 찾는다)
        outfile = next((a for a in argv[2:] if a.endswith("diagnosis_output.json")), None)
        if outfile:
            Path(outfile).write_text(json.dumps({"summary": "fake diagnosis", "likely_cause": "unknown"}), encoding="utf-8")
    elif (service, op) == ("sts", "get-caller-identity"):
        print(json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/fake"}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""가짜 aws CLI: 호출 내용을 기록하고 정해진 응답을 돌려준다 (AWS 없이 워커를 시험할 때).

사용법 (PAWPLOY_OFFLINE 은 끄고):
  AWS_BIN=tools/fake-aws.py FAKE_AWS_LOG=/tmp/aws.jsonl python -m tfworker deploy examples/job-ec2.json

흉내 내는 명령
  ecr describe-images                      → 이미지 1건 (digest sha256:aaaa…). FAKE_AWS_ECR_MISSING=1 이면 빈 결과
  dynamodb put-item / get-item / delete-item → FAKE_AWS_DB(JSON 파일)에 저장. 잠금용 조건식
                                             (attribute_not_exists·lease_until < :now / #o = :me)을 흉내 낸다.
                                             조건이 맞지 않으면 ConditionalCheckFailedException (exit 254)
  dynamodb scan                            → FAKE_AWS_SCAN_ITEMS (JSON 배열) 를 Items 로
  sqs receive-message / delete-message     → FAKE_AWS_SQS(JSON 파일, 메시지 본문 배열)에서 꺼내고 지운다
  s3 sync / s3 cp                          → 빈 출력 (cp 대상이 "-" 면 FAKE_AWS_S3_BODY)
                                             FAKE_AWS_S3_ROOT 가 있으면 s3://버킷/키 = <ROOT>/버킷/키 로 sync·list-objects-v2
  resourcegroupstaggingapi get-resources   → FAKE_AWS_TAGGED (JSON 배열) 를 ResourceTagMappingList 로
  ec2 get-console-output / logs tail       → 가짜 로그 텍스트
  sts get-caller-identity                  → 계정 123456789012

환경변수
  FAKE_AWS_LOG   호출 argv 를 한 줄에 하나씩 JSON 으로 덧붙여 기록할 파일 (테스트가 검증에 씀)
  FAKE_AWS_FAIL  "ecr describe-images" 처럼 "<서비스> <명령>" 이 일치하면 exit 1
"""
import json
import os
import sys
import shutil
from pathlib import Path

DIGEST = "sha256:" + "a" * 64


def _load(env: str, default):
    path = os.environ.get(env)
    if path and Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return default


def _save(env: str, data) -> None:
    path = os.environ.get(env)
    if path:
        Path(path).write_text(json.dumps(data), encoding="utf-8")


def _conditional_failed(op: str) -> int:
    print(f"An error occurred (ConditionalCheckFailedException) when calling the {op} operation: "
          "The conditional request failed", file=sys.stderr)
    return 254


def _dynamodb(op: str, arg) -> int:
    db = _load("FAKE_AWS_DB", {})
    condition = arg("--condition-expression")
    values = json.loads(arg("--expression-attribute-values") or "{}")
    if op == "put-item":
        item = json.loads(arg("--item"))
        key = item["deploy_id"]["S"]
        old = db.get(key)
        if condition and old is not None:   # attribute_not_exists(deploy_id) OR lease_until < :now
            if not int(old.get("lease_until", {}).get("N", "0")) < int(values[":now"]["N"]):
                return _conditional_failed("PutItem")
        db[key] = item
        _save("FAKE_AWS_DB", db)
        print("{}")
    elif op == "get-item":
        key = json.loads(arg("--key"))["deploy_id"]["S"]
        print(json.dumps({"Item": db[key]} if key in db else {}))
    elif op == "delete-item":
        key = json.loads(arg("--key"))["deploy_id"]["S"]
        old = db.get(key)
        if condition and (old is None or old.get("owner") != values.get(":me")):   # #o = :me
            return _conditional_failed("DeleteItem")
        db.pop(key, None)
        _save("FAKE_AWS_DB", db)
        print("{}")
    return 0


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
    elif service == "dynamodb" and op in ("put-item", "get-item", "delete-item"):
        return _dynamodb(op, arg)
    elif (service, op) == ("sqs", "receive-message"):
        bodies = _load("FAKE_AWS_SQS", [])
        _save("FAKE_AWS_SQS", [])
        print(json.dumps({"Messages": [{"Body": b, "ReceiptHandle": f"rh-{i}"} for i, b in enumerate(bodies)]}
                         if bodies else {}))
    elif (service, op) == ("dynamodb", "scan"):
        print(json.dumps({"Items": json.loads(os.environ.get("FAKE_AWS_SCAN_ITEMS", "[]")), "Count": 0}))
    elif (service, op) == ("s3api", "list-objects-v2") and os.environ.get("FAKE_AWS_S3_ROOT"):
        prefix = arg("--prefix", "")
        folder = Path(os.environ["FAKE_AWS_S3_ROOT"]) / arg("--bucket") / prefix
        subdirs = sorted(d.name for d in folder.iterdir() if d.is_dir()) if folder.is_dir() else []
        print(json.dumps({"CommonPrefixes": [{"Prefix": f"{prefix}{d}/"} for d in subdirs]} if subdirs else {}))
    elif service == "s3":
        if op == "cp" and len(argv) > 3 and argv[3] == "-":
            print(os.environ.get("FAKE_AWS_S3_BODY", "{}"))
        elif op == "sync" and argv[2].startswith("s3://") and os.environ.get("FAKE_AWS_S3_ROOT"):
            src = Path(os.environ["FAKE_AWS_S3_ROOT"]) / argv[2][len("s3://"):]
            if src.is_dir():
                shutil.copytree(src, argv[3], dirs_exist_ok=True)
    elif (service, op) == ("resourcegroupstaggingapi", "get-resources"):
        print(json.dumps({"ResourceTagMappingList": json.loads(os.environ.get("FAKE_AWS_TAGGED", "[]"))}))
    elif (service, op) == ("ec2", "get-console-output"):
        print(json.dumps({"InstanceId": arg("--instance-id"), "Output": "[pawploy] fake console output"}))
    elif (service, op) == ("logs", "tail"):
        print("fake lambda log line")
    elif (service, op) == ("sts", "get-caller-identity"):
        print(json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/fake"}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

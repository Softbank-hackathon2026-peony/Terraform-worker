# EC2 + Docker Compose 아키텍처: 인스턴스 1대에서 앱의 컨테이너 여러 개(앱·워커·DB·캐시·프록시 …)를 실행
# 입력: name, images, size, health_path   (컨테이너 구성·포트·환경변수는 compose.yaml.tftpl)
# 출력: endpoint, health_url, resource_id (다른 아키텍처와 같음)
#
# compose.yaml.tftpl 은 AgentCore 코드가 InfraFit deploy_units 로 앱마다 렌더해 이 폴더에 둔다 (여기 있는 것은 예시).
# 템플릿에 넘기는 값 (README "ec2_compose 템플릿 약속"):
#   images    map(string)  이미지 id → digest 고정 ECR 주소 (작업 입력 targets[].images 그대로)
#   passwords map(string)  비밀번호 id → 이 모듈의 random_password 결과 (영문·숫자 24자)
#                          템플릿에 적힌 passwords["<id>"] 를 찾아 id 마다 하나씩 만든다
# 보안·비용은 modules/ec2 와 같다: 기본 VPC, 80번 포트만, ECR 읽기 권한만, IMDSv2, 디스크 암호화, CPU 크레딧 standard

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    random = {
      source = "hashicorp/random"
    }
  }
}

variable "name" { type = string }
variable "images" { type = map(string) }
variable "size" { type = string }
variable "health_path" { type = string }

locals {
  instance_types = {
    micro  = "t3.micro"
    small  = "t3.small"
    medium = "t3.medium"
  }

  # 템플릿이 쓰는 비밀번호 id (passwords["postgres"] → "postgres"). 같은 id 는 어디에 쓰든 같은 값
  password_ids = toset(flatten(regexall("passwords\\[\"([A-Za-z0-9_.-]+)\"\\]", file("${path.module}/compose.yaml.tftpl"))))

  compose = templatefile("${path.module}/compose.yaml.tftpl", {
    images    = var.images
    passwords = { for id, p in random_password.datastore : id => p.result }
  })

  # ECR 레지스트리 → 리전 (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com → ap-northeast-2). 레지스트리마다 한 번 로그인
  registries = {
    for r in distinct([for uri in values(var.images) : split("/", uri)[0]]) :
    r => try(regex("\\.dkr\\.ecr\\.([a-z0-9-]+)\\.amazonaws\\.com$", r)[0], "")
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    registries     = local.registries
    compose_gz_b64 = base64gzip(local.compose)
  })
}

# ---------------- 데이터 저장소 컨테이너 비밀번호 ----------------

# URL(postgresql://user:<비밀번호>@host/db)·YAML 에 그대로 넣을 수 있게 특수문자 없이
resource "random_password" "datastore" {
  for_each = local.password_ids
  length   = 24
  special  = false
}

# ---------------- 네트워크: 기본 VPC의 기본 서브넷 ----------------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# ---------------- 최신 ECS-optimized Amazon Linux 2023 (Docker·AWS CLI 포함) ----------------
# 일반 AL2023 은 부팅 때 dnf install docker 로 60~97초를 써서(운영 실측) Docker 가 들어 있는 AMI 를 쓴다.
# 이 AMI 의 루트 스냅샷은 30GB 라 root_block_device 도 30GB 이상이어야 한다.

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

# ---------------- 보안 그룹: 80번 포트만 열기 (SSH 닫음) ----------------
# 컨테이너끼리는 Docker 내부 네트워크로 통신하므로 DB·캐시 포트는 열지 않는다

resource "aws_security_group" "app" {
  name_prefix = "${var.name}-"
  description = "Pawploy test app: HTTP only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "http"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------- IAM: ECR에서 이미지 읽기만 ----------------

# IAM name_prefix 는 최대 38자 → 이름 37자 + "-" (modules/ec2 와 같음)
resource "aws_iam_role" "app" {
  name_prefix = "${substr(var.name, 0, 37)}-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_instance_profile" "app" {
  name_prefix = "${substr(var.name, 0, 37)}-"
  role        = aws_iam_role.app.name
}

# ---------------- 인스턴스 ----------------

resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = local.instance_types[var.size]
  subnet_id                   = data.aws_subnets.default.ids[0]
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true
  user_data                   = local.user_data
  user_data_replace_on_change = true

  # CPU를 계속 써도 추가 요금이 나지 않도록 (기본값 unlimited는 추가 과금 가능)
  credit_specification {
    cpu_credits = "standard"
  }

  # IMDSv2 + 홉 1: 컨테이너(브리지 네트워크)에서는 인스턴스 역할 자격 증명을 받을 수 없게
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # 이미지 여러 개 + DB 데이터 → modules/ec2(20GB)보다 크게
  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    encrypted   = true
  }

  tags = {
    Name = var.name
  }

  depends_on = [aws_iam_role_policy_attachment.ecr_read]
}

# ---------------- 출력 ----------------

output "endpoint" {
  value = "http://${aws_instance.app.public_ip}"
}

output "health_url" {
  value = "http://${aws_instance.app.public_ip}${var.health_path}"
}

output "resource_id" {
  value = aws_instance.app.id
}

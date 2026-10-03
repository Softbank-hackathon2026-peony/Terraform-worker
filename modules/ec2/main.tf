# EC2 아키텍처: Amazon Linux 2023 + Docker로 컨테이너 1개 실행
# 공통 입력: name, image_uri, container_port, size, env, health_path
# 공통 출력: endpoint, health_url, resource_id

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "name" { type = string }
variable "image_uri" { type = string }
variable "container_port" { type = number }
variable "size" { type = string }
variable "env" { type = map(string) }
variable "health_path" { type = string }

locals {
  instance_types = {
    micro  = "t3.micro"
    small  = "t3.small"
    medium = "t3.medium"
  }

  # ECR 주소에서 레지스트리와 리전 추출 (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/app@sha256:...)
  registry   = split("/", var.image_uri)[0]
  ecr_region = try(regex("\\.dkr\\.ecr\\.([a-z0-9-]+)\\.amazonaws\\.com$", local.registry)[0], "")

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    image_uri      = var.image_uri
    registry       = local.registry
    ecr_region     = local.ecr_region
    container_port = var.container_port
    env            = var.env
  })
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

# 이름에 deploy_id를 넣어(pawploy-<deploy_id>-xxxx) 콘솔에서 어느 배포의 역할인지 바로 알 수 있게 한다.
# IAM 이름 제한 64자에서 Terraform 이 붙이는 접미사 26자를 빼면 name_prefix 는 최대 38자다.
# 끝에 "-" 를 붙이므로 이름은 37자로 자른다 (37 + 1 = 38).
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

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 30 # ECS-optimized AMI 스냅샷 크기 이상
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

# Lambda 아키텍처: 컨테이너 이미지 + 함수 URL
# 이미지에는 AWS Lambda Web Adapter가 들어 있어야 일반 웹앱이 동작함 (Build Worker 담당과 약속)
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
  memory = {
    micro  = 512
    small  = 1024
    medium = 2048
  }
}

# EC2 모듈과 같은 규칙으로 이름에 deploy_id 를 넣는다 (콘솔에서 어느 배포의 역할인지 바로 알 수 있게).
# IAM 이름 제한 64자 때문에 name_prefix 는 38자로 자른다 (modules/ec2 참고).
resource "aws_iam_role" "app" {
  name_prefix = "${substr(var.name, 0, 38)}-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# 로그 쓰기만 허용
resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "app" {
  function_name = var.name
  role          = aws_iam_role.app.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["x86_64"]
  timeout       = 30
  memory_size   = local.memory[var.size]

  environment {
    variables = merge(var.env, {
      PORT         = tostring(var.container_port)
      AWS_LWA_PORT = tostring(var.container_port) # Lambda Web Adapter가 앱으로 넘길 포트
    })
  }

  depends_on = [aws_iam_role_policy_attachment.logs]
}

resource "aws_lambda_function_url" "app" {
  function_name      = aws_lambda_function.app.function_name
  authorization_type = "NONE"
}

# 인증 없는 함수 URL을 API/Terraform으로 만들 때는 공개 호출 권한을 따로 추가해야 함.
# 2025-10부터 새 함수 URL은 lambda:InvokeFunctionUrl 과 lambda:InvokeFunction 둘 다 있어야 하며,
# 하나라도 빠지면 auth type이 NONE이어도 403 Forbidden이 난다.
# (https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html)
resource "aws_lambda_permission" "public_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.app.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# invoked_via_function_url 조건으로 "함수 URL을 통해서만" 호출을 허용한다 (직접 Invoke API는 막힘)
resource "aws_lambda_permission" "public_invoke" {
  statement_id             = "AllowPublicInvokeViaFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.app.function_name
  principal                = "*"
  invoked_via_function_url = true
}

output "endpoint" {
  value = trimsuffix(aws_lambda_function_url.app.function_url, "/")
}

output "health_url" {
  value = "${trimsuffix(aws_lambda_function_url.app.function_url, "/")}${var.health_path}"
}

output "resource_id" {
  value = aws_lambda_function.app.arn
}

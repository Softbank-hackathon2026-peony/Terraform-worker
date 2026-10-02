# GCP Cloud Run 아키텍처: 컨테이너 1개를 서버리스로 실행 + 공개 URL
# 공통 입력: name, image_uri, container_port, size, env, health_path
# 공통 출력: endpoint, health_url, resource_id
# 이미지는 같은 프로젝트의 Artifact Registry 에 있어야 한다 (CodeBuild 가 ECR 과 함께 푸시)

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
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
    micro  = "512Mi"
    small  = "1Gi"
    medium = "2Gi"
  }
  # 서비스 계정 ID 는 6~30자, 끝이 하이픈이면 안 된다
  account_id = replace(substr(replace(var.name, "pawploy-", "pp-"), 0, 30), "/-+$/", "")
}

# 앱 전용 서비스 계정: 역할을 하나도 주지 않는다.
# 지정하지 않으면 Compute 기본 서비스 계정(편집자 권한)으로 실행돼 우리 프로젝트를 건드릴 수 있다
resource "google_service_account" "app" {
  account_id   = local.account_id
  display_name = "Pawploy ${var.name}"
}

# provider 에 설정된 프로젝트·리전 (루트 main.tf 에서 워커가 정한다)
data "google_client_config" "current" {}

resource "google_cloud_run_v2_service" "app" {
  name     = var.name
  location = data.google_client_config.current.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # 기본값 true 이면 terraform destroy 가 실패한다. 삭제가 배포보다 중요하므로 반드시 false
  deletion_protection = false

  template {
    service_account = google_service_account.app.email
    timeout         = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = 1 # 악용 방지
    }

    containers {
      image = var.image_uri

      ports {
        container_port = var.container_port # Cloud Run 이 PORT 환경변수를 자동으로 넣는다 (PORT 는 직접 넣으면 거부됨)
      }

      resources {
        limits = {
          cpu    = "1"
          memory = local.memory[var.size]
        }
        cpu_idle = true # 요청이 없을 때 CPU 과금 안 함
      }

      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# 인증 없이 누구나 접속 (테스트 환경). 조직 정책이 allUsers 를 막으면 여기서 실패한다
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "endpoint" {
  value = google_cloud_run_v2_service.app.uri
}

output "health_url" {
  value = "${google_cloud_run_v2_service.app.uri}${var.health_path}"
}

output "resource_id" {
  value = google_cloud_run_v2_service.app.id
}

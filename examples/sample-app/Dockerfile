# EC2와 Lambda 둘 다에서 동작하는 테스트 이미지
FROM python:3.12-slim

# Lambda Web Adapter: Lambda에서는 이걸 통해 일반 웹앱이 동작하고, EC2에서는 무시됨
# 버전은 공식 README(awslabs/aws-lambda-web-adapter)가 권장하는 태그를 따른다 (2026-10 기준 1.1.0)
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app
COPY app.py .
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]

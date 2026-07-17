FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "fastapi>=0.115,<1" \
    "uvicorn[standard]>=0.34,<1"

WORKDIR /app
COPY infra/policy_target/app.py /app/app.py

USER 65532:65532
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]

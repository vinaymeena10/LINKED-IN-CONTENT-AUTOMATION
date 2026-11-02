FROM python:3.11-slim
WORKDIR /app
COPY L_requirements.txt ./L_requirements.txt
RUN pip install --no-cache-dir -r L_requirements.txt
COPY . .
RUN mkdir -p static/generated
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]

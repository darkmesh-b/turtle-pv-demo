FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080
WORKDIR /app
COPY app.py index.html ./
RUN mkdir -p /data && chgrp -R 0 /app /data && chmod -R g=u /app /data
USER 10001:0
EXPOSE 8080
CMD ["python", "/app/app.py"]

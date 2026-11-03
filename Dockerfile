FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY app ./app
COPY web ./web
RUN pip install --no-cache-dir .
ENV URJA_DB_PATH=/data/urja.sqlite
VOLUME ["/data"]
EXPOSE 8000
# Credentials come from the environment at run time (never baked into the image).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

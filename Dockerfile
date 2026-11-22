FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY data ./data
ENTRYPOINT ["geodataops"]
CMD ["run"]

FROM runtime AS test
RUN pip install --no-cache-dir '.[test]'
COPY tests ./tests
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY relationship_graph relationship_graph
COPY fixtures fixtures
RUN pip install --no-cache-dir '.[live]'
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "relationship_graph.api:app", "--host", "0.0.0.0", "--port", "8000"]


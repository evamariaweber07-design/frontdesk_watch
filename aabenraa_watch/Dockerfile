FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --no-dev

COPY frontdesk_watch.py ./

CMD ["uv", "run", "python", "-u", "frontdesk_watch.py"]

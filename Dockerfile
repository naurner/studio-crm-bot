# Образ держит только Python и зависимости: сам код приезжает из папки
# проекта (bind-mount в docker-compose.yml). Поэтому «🔄 Обновить с GitHub»
# внутри контейнера работает обычным git pull, без пересборки образа —
# пересбор нужен, только если изменился pyproject.toml.
FROM python:3.12-slim

# git — для обновления из бота, tzdata — чтобы TZ из compose реально применялся
# (иначе внутри контейнера UTC и напоминания уедут).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Ставим только зависимости из pyproject.toml, без кода: слой кэшируется и не
# пересобирается при каждом изменении бота.
COPY pyproject.toml ./
RUN mkdir -p src/crmlumi \
    && touch src/crmlumi/__init__.py \
    && pip install --no-cache-dir . \
    && rm -rf src build *.egg-info

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    CRMLUMI_SUPERVISED=1 \
    CRMLUMI_RUNTIME=docker

# git отказывается работать в каталоге с «чужим» владельцем — на bind-mount
# с Mac это обычное дело.
RUN git config --global --add safe.directory /app

CMD ["python", "-m", "crmlumi", "run"]

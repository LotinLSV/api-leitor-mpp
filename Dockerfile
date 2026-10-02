FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# JRE headless (suficiente para rodar o MPXJ; menor que o JDK).
# O symlink /opt/java deixa o JAVA_HOME independente da arquitetura (amd64/arm64).
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
    && ln -sfn "$(dirname "$(dirname "$(readlink -f "$(which java)")")")" /opt/java \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/opt/java
ENV PATH="${JAVA_HOME}/bin:${PATH}"

WORKDIR /app

# Dependências primeiro (aproveita cache de camadas).
# O pacote "mpxj" do PyPI já traz o mpxj.jar + todas as libs dependentes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data

# O Render injeta a variável PORT (padrão 10000).
ENV PORT=10000
EXPOSE 10000

# exec: uvicorn vira PID 1 e recebe SIGTERM corretamente no deploy.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]

# ---------- Estágio 1: build ----------
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ---------- Estágio 2: runtime ----------
FROM python:3.11-slim

# JDK headless (menor footprint que o JDK completo)
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:$PATH"

WORKDIR /app

COPY --from=builder /root/.local /root/.local
ENV PATH="/root/.local/bin:$PATH"

# Baixa o MPXJ (JAR com dependências inclusas)
ARG MPXJ_VERSION=12.10.0
RUN mkdir -p /app/libs && \
    curl -L -o /app/libs/mpxj.jar \
      "https://repo1.maven.org/maven2/net/sf/mpxj/mpxj/${MPXJ_VERSION}/mpxj-${MPXJ_VERSION}-jar-with-dependencies.jar" && \
    ls -lh /app/libs/mpxj.jar

COPY . .

# Diretório de dados (será sobrescrito pelo disco persistente, se houver)
RUN mkdir -p /app/data

EXPOSE 8000

# Limita a JVM para caber nos 512 MB do Render free tier
ENV JAVA_OPTS="-Xms64m -Xmx320m -XX:+UseSerialGC"

# Porta dinâmica: Render injeta $PORT em runtime
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

# ---------- Estágio 1: build ----------
FROM python:3.11-slim AS builder

WORKDIR /app

# Dependências de sistema para compilar pacotes Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ---------- Estágio 2: runtime ----------
FROM python:3.11-slim

# Instala o JDK (necessário para JPype + MPXJ)
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jdk-headless \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Configura JAVA_HOME
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="$JAVA_HOME/bin:$PATH"

# Diretório da aplicação
WORKDIR /app

# Copia pacotes Python instalados no estágio builder
COPY --from=builder /root/.local /root/.local
ENV PATH="/root/.local/bin:$PATH"

# Baixa o JAR do MPXJ (troque pela versão mais recente)
# Fonte: https://sourceforge.net/projects/mpxj/files/
ARG MPXJ_VERSION=12.10.0
RUN mkdir -p /app/libs && \
    curl -L -o /app/libs/mpxj.jar \
      "https://sourceforge.net/projects/mpxj/files/mpxj/${MPXJ_VERSION}/mpxj-${MPXJ_VERSION}.jar/download" && \
    ls -lh /app/libs/mpxj.jar

# Também baixa as dependências do MPXJ (Apache POI, etc.)
# Se você já tem o JAR "fat" (mpxj-x.x.x-jar-with-dependencies.jar), use ele e remova esse bloco.
RUN curl -L -o /app/libs/mpxj-deps.jar \
      "https://repo1.maven.org/maven2/net/sf/mpxj/mpxj/${MPXJ_VERSION}/mpxj-${MPXJ_VERSION}-jar-with-dependencies.jar" || true

# Classpath da JVM
ENV CLASSPATH="/app/libs/mpxj.jar:/app/libs/mpxj-deps.jar"

# Copia o código da aplicação
COPY . .

# Expõe a porta
EXPOSE 8000

# Variável da API Key (sobrescreva em runtime)
ENV API_KEY="troque-esta-chave-em-producao"

# IMPORTANTE: --workers 1 por causa do JPype (uma JVM por processo)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

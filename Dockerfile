# Fixa a versão do Debian para evitar quebras futuras
FROM python:3.10-slim-bookworm

# Evita prompts interativos durante a instalação
ENV DEBIAN_FRONTEND=noninteractive

# Instala o Java (default-jre é mais compatível que openjdk-17-jre-headless)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    default-jre-headless \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Define diretório de trabalho
WORKDIR /app

# Copia e instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da API
COPY main.py .

# Limita memória da JVM (Render free tier tem 512 MB)
ENV JAVA_TOOL_OPTIONS="-Xmx256m -Xms64m"

# Render injeta a variável PORT automaticamente
EXPOSE 80

# Inicia a API escutando em 0.0.0.0 na porta definida pelo Render
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-80}"]

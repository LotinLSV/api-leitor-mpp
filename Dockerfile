# Utiliza uma imagem oficial do Python como base
FROM python:3.10-slim

# Instala o Java (JRE), necessário para o funcionamento do MPXJ
RUN apt-get update && apt-get install -y \
    default-jre \
    && rm -rf /var/lib/apt/lists/*

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Copia os arquivos de requisitos e instala as dependências
RUN pip install --no-cache-dir fastapi uvicorn mpxj jpype1 python-multipart

# Copia o código da sua API para o container
COPY main.py /app/main.py

# Expõe a porta padrão que o Azure App Service escuta
EXPOSE 80

# Comando para rodar a API usando o Uvicorn na porta 80
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]

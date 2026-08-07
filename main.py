import os
import json
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
import jpype
import mpxj

# 1. A JVM PRECISA SER INICIADA ANTES DE IMPORTAR OS PACOTES JAVA (org.*)
if not jpype.isJVMStarted():
    # Passa o classpath do mpxj para a JVM se necessário ou usa a inicialização padrão
    jpype.startJVM(jpype.getDefaultJVMPath())

# 2. SOMENTE APÓS A JVM ESTAR RODANDO, FAÇA A IMPORTAÇÃO DO MPXJ
from org.mpxj.mpp import MPPReader # type: ignore

app = FastAPI(
    title="API Leitor MPP", 
    description="Lê arquivos do Microsoft Project (.mpp) e retorna JSON estruturado"
)

# ... seu código dos endpoints continua aqui ...


# ... restante do seu código

# Banco de dados em memória temporária para guardar os dados processados
# (Para produção persistente, considere conectar a um banco como PostgreSQL/SQLite)
DB_PROJETOS: Dict[str, dict] = {}


@app.post("/ler-mpp/")
async def ler_arquivo_mpp(file: UploadFile = File(...)):
    if not (file.filename.endswith(".mpp") or file.filename.endswith(".xml")):
        raise HTTPException(status_code=400, detail="Envie um arquivo .mpp ou .xml válido.")
    
    temp_path = f"temp_{file.filename}"
    with open(temp_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
        
    try:
        reader = MPPReader()
        project = reader.read(temp_path)
        
        tarefas = []
        for task in project.getTasks():
            nome_tarefa = str(task.getName()).strip() if task.getName() is not None else ""
            
            if nome_tarefa:
                duracao = task.getDuration()
                duracao_valor = duracao.getDuration() if duracao else 0
                duracao_unidade = str(duracao.getUnits()) if duracao else "Days"

                tarefas.append({
                    "id": task.getID(),
                    "unique_id": task.getUniqueID(),
                    "nome_arquivo": file.filename,
                    "taskname": nome_tarefa,
                    "inicio": str(task.getStart()) if task.getStart() else None,
                    "termino": str(task.getFinish()) if task.getFinish() else None,
                    "percentual_concluido": float(task.getPercentageComplete()) if task.getPercentageComplete() else 0.0,
                    "duracao": duracao_valor,
                    "unidade_duracao": duracao_unidade,
                    "custo": float(task.getCost()) if task.getCost() else 0.0,
                    "eh_critico": bool(task.getCritical())
                })
                
        dados_projeto = {
            "nome_arquivo": file.filename,
            "total_tarefas": len(tarefas),
            "tarefas": tarefas
        }
        
        # Grava os dados no DB em memória chaveado pelo nome do arquivo
        DB_PROJETOS[file.filename] = dados_projeto
        
        return dados_projeto

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo: {str(e)}")
    
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ------------------------------------------------------------------
# NOVO ENDPOINT GET PARA O POWER BI CONSUMIR
# ------------------------------------------------------------------
@app.get("/projetos/")
async def obter_todos_projetos():
    """
    Retorna uma lista consolidada de todas as tarefas cadastradas.
    Este endpoint é lido nativamente pelo Power BI através do conector Web.
    """
    todas_tarefas = []
    for nome_arquivo, projeto in DB_PROJETOS.items():
        todas_tarefas.extend(projeto["tarefas"])
        
    return todas_tarefas
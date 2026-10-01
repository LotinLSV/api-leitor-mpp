from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import jpype
import mpxj
import json
import os
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

# ------------------------------------------------------------------
# CONFIGURAÇÃO DE PERSISTÊNCIA
# ------------------------------------------------------------------
ARQUIVO_DADOS = "dados_mpp.json"

def carregar_dados():
    if os.path.exists(ARQUIVO_DADOS):
        with open(ARQUIVO_DADOS, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"projetos": {}, "ultima_atualizacao": None}

def salvar_dados(dados):
    dados["ultima_atualizacao"] = datetime.now().isoformat()
    with open(ARQUIVO_DADOS, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

# Inicia a Máquina Virtual Java necessária para o MPXJ rodar no Python
if not jpype.isJVMStarted():
    jpype.startJVM()

# Importações dinâmicas do MPXJ
from org.mpxj.mpp import MPPReader  # type: ignore

app = FastAPI(
    title="API Leitor MPP para Power BI",
    description="Lê arquivos .mpp via POST e expõe dados via GET para consumo no Power BI",
    version="2.0"
)

# CORS — necessário se o Power BI Service/Online for consumir diretamente
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# MODELOS DE DADOS
# ------------------------------------------------------------------
class TarefaUpdate(BaseModel):
    unique_id: int
    taskname: Optional[str] = None
    percentual_concluido: Optional[float] = 0.0
    duracao: Optional[float] = None
    inicio: Optional[str] = None
    termino: Optional[str] = None

class ProjetoUpdate(BaseModel):
    nome_arquivo: str
    tarefas: List[TarefaUpdate]


# ------------------------------------------------------------------
# 1. ENDPOINT DE LEITURA (POST) — CHAMADO PELO POWER AUTOMATE
# ------------------------------------------------------------------
@app.post("/ler-mpp/")
async def ler_arquivo_mpp(file: UploadFile = File(...)):
    if not file.filename.endswith(".mpp"):
        raise HTTPException(status_code=400, detail="Envie um arquivo com extensão .mpp válido.")

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

                possui_entrega = bool(task.getMilestone()) or float(duracao_valor) == 0.0

                parent_task = task.getParentTask()
                eh_tarefa_mae = bool(task.getSummary())
                id_tarefa_mae = parent_task.getID() if parent_task else None
                nome_tarefa_mae = str(parent_task.getName()).strip() if parent_task and parent_task.getName() else None
                tipo_hierarquia = "mae" if eh_tarefa_mae else ("filha" if parent_task else "independente")

                recursos = []
                for assignment in task.getResourceAssignments():
                    resource = assignment.getResource()
                    if resource and resource.getName():
                        recursos.append(str(resource.getName()))

                predecessoras = []
                if task.getPredecessors():
                    for relation in task.getPredecessors():
                        pred_task = None
                        if hasattr(relation, 'getPredecessorTask'):
                            pred_task = relation.getPredecessorTask()
                        elif hasattr(relation, 'getSourceTask'):
                            pred_task = relation.getSourceTask()

                        if pred_task:
                            predecessoras.append({
                                "id_tarefa": pred_task.getID(),
                                "tipo_vinculo": str(relation.getType())
                            })

                sucessoras = []
                if task.getSuccessors():
                    for relation in task.getSuccessors():
                        suc_task = None
                        if hasattr(relation, 'getSuccessorTask'):
                            suc_task = relation.getSuccessorTask()
                        elif hasattr(relation, 'getTargetTask'):
                            suc_task = relation.getTargetTask()
                        elif hasattr(relation, 'getTask'):
                            suc_task = relation.getTask()

                        if suc_task:
                            sucessoras.append({
                                "id_tarefa": suc_task.getID(),
                                "tipo_vinculo": str(relation.getType())
                            })

                tarefas.append({
                    "id": task.getID(),
                    "unique_id": task.getUniqueID(),
                    "nome_arquivo": file.filename,
                    "taskname": nome_tarefa,
                    "inicio": str(task.getStart()) if task.getStart() else None,
                    "termino": str(task.getFinish()) if task.getFinish() else None,
                    "inicio_real": str(task.getActualStart()) if task.getActualStart() else None,
                    "termino_real": str(task.getActualFinish()) if task.getActualFinish() else None,
                    "percentual_concluido": float(task.getPercentageComplete()) if task.getPercentageComplete() else 0.0,
                    "duracao": duracao_valor,
                    "unidade_duracao": duracao_unidade,
                    "custo": float(task.getCost()) if task.getCost() else 0.0,
                    "trabalho_horas": task.getWork().getDuration() if task.getWork() else 0,
                    "eh_marco": bool(task.getMilestone()),
                    "possui_entrega": possui_entrega,
                    "eh_tarefa_mae": eh_tarefa_mae,
                    "eh_tarefa_filha": bool(parent_task),
                    "id_tarefa_mae": id_tarefa_mae,
                    "nome_tarefa_mae": nome_tarefa_mae,
                    "tipo_hierarquia": tipo_hierarquia,
                    "eh_critico": bool(task.getCritical()),
                    "recursos_atribuidos": recursos,
                    "predecessoras": predecessoras,
                    "sucessoras": sucessoras,
                    "custo_previsto": float(task.getCost()) if task.getCost() else 0.0,
                    "custo_realizado": float(task.getActualCost()) if task.getActualCost() else 0.0,
                    "custo": float(task.getCost()) if task.getCost() else 0.0,
                    "notas": str(task.getNotes()) if task.getNotes() else ""
                })

        # Persiste no arquivo JSON
        db = carregar_dados()
        db["projetos"][file.filename] = {
            "nome_arquivo": file.filename,
            "total_tarefas": len(tarefas),
            "data_processamento": datetime.now().isoformat(),
            "tarefas": tarefas
        }
        salvar_dados(db)

        return {
            "status": "sucesso",
            "nome_arquivo": file.filename,
            "total_tarefas": len(tarefas)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo MPP: {str(e)}")

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ------------------------------------------------------------------
# 2. ENDPOINTS DE CONSUMO (GET) — CHAMADOS PELO POWER BI
# ------------------------------------------------------------------

@app.get("/tarefas/")
async def listar_todas_tarefas(
    nome_arquivo: Optional[str] = Query(None, description="Filtrar por nome do arquivo .mpp"),
    eh_tarefa_mae: Optional[bool] = Query(None, description="Filtrar apenas tarefas mãe (summary)"),
    eh_critico: Optional[bool] = Query(None, description="Filtrar apenas tarefas do caminho crítico")
):
    """
    Retorna TODAS as tarefas de TODOS os projetos no formato flat (tabela).
    Ideal para consumo direto no Power BI via conector Web.
    """
    db = carregar_dados()
    todas_tarefas = []

    for projeto_nome, projeto_data in db.get("projetos", {}).items():
        for tarefa in projeto_data.get("tarefas", []):
            # Aplica filtros opcionais
            if nome_arquivo and tarefa.get("nome_arquivo") != nome_arquivo:
                continue
            if eh_tarefa_mae is not None and tarefa.get("eh_tarefa_mae") != eh_tarefa_mae:
                continue
            if eh_critico is not None and tarefa.get("eh_critico") != eh_critico:
                continue
            todas_tarefas.append(tarefa)

    return {
        "total_registros": len(todas_tarefas),
        "ultima_atualizacao": db.get("ultima_atualizacao"),
        "tarefas": todas_tarefas
    }


@app.get("/projetos/")
async def listar_projetos():
    """
    Retorna a lista de projetos (arquivos .mpp) já processados.
    Útil para criar filtros no Power BI.
    """
    db = carregar_dados()
    resumo = []

    for nome, dados in db.get("projetos", {}).items():
        resumo.append({
            "nome_arquivo": nome,
            "total_tarefas": dados.get("total_tarefas", 0),
            "data_processamento": dados.get("data_processamento")
        })

    return {
        "total_projetos": len(resumo),
        "ultima_atualizacao": db.get("ultima_atualizacao"),
        "projetos": resumo
    }


@app.get("/projetos/{nome_arquivo}/tarefas/")
async def tarefas_por_projeto(nome_arquivo: str):
    """
    Retorna as tarefas de um projeto específico.
    Substitua espaços por %20 na URL.
    """
    db = carregar_dados()
    projeto = db.get("projetos", {}).get(nome_arquivo)

    if not projeto:
        raise HTTPException(
            status_code=404, 
            detail=f"Projeto '{nome_arquivo}' não encontrado. Use /projetos/ para listar os disponíveis."
        )

    return {
        "nome_arquivo": nome_arquivo,
        "total_tarefas": projeto.get("total_tarefas", 0),
        "data_processamento": projeto.get("data_processamento"),
        "tarefas": projeto.get("tarefas", [])
    }


@app.get("/health/")
async def health_check():
    """Endpoint simples para verificar se a API está no ar."""
    db = carregar_dados()
    return {
        "status": "online",
        "total_projetos": len(db.get("projetos", {})),
        "ultima_atualizacao": db.get("ultima_atualizacao")
    }


# ------------------------------------------------------------------
# 3. ENDPOINT PARA LIMPEZA (ÚTIL EM DESENVOLVIMENTO)
# ------------------------------------------------------------------
@app.delete("/projetos/{nome_arquivo}/")
async def deletar_projeto(nome_arquivo: str):
    """Remove um projeto da base de dados."""
    db = carregar_dados()
    if nome_arquivo in db.get("projetos", {}):
        del db["projetos"][nome_arquivo]
        salvar_dados(db)
        return {"status": "deletado", "nome_arquivo": nome_arquivo}
    raise HTTPException(status_code=404, detail="Projeto não encontrado.")

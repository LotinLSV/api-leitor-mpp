from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import jpype
import json
import os
import uuid
from typing import List, Optional, Any
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
        json.dump(dados, f, ensure_ascii=False, indent=2, default=str)

# ------------------------------------------------------------------
# HELPERS DE CONVERSÃO JAVA -> PYTHON  (ESSENCIAL!)
# ------------------------------------------------------------------
def safe_str(value: Any, default: str = "") -> str:
    """Converte qualquer objeto Java para string, tratando null."""
    if value is None:
        return default
    try:
        s = str(value).strip()
    except Exception:
        return default
    # JPype às vezes devolve a string literal "null"
    if s.lower() in ("null", "none", "nan"):
        return default
    return s

def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def safe_bool(value: Any) -> bool:
    if value is None:
        return False
    try:
        return bool(value)
    except Exception:
        return False

def safe_date(value: Any) -> Optional[str]:
    """Formata datas do MPXJ (LocalDateTime, LocalDate ou java.util.Date) em ISO 8601."""
    if value is None:
        return None
    # java.time.LocalDateTime / LocalDate / OffsetDateTime
    if hasattr(value, "toString"):
        try:
            s = value.toString()
            if s and s.lower() != "null":
                # Normaliza "2024-01-01T08:00" -> "2024-01-01T08:00:00"
                if "T" in s and len(s) == 16:
                    s += ":00"
                return s
        except Exception:
            pass
    # java.util.Date (fallback)
    try:
        return value.isoformat()
    except Exception:
        return safe_str(value) or None

# ------------------------------------------------------------------
# INICIA JVM (MPXJ)
# ------------------------------------------------------------------
if not jpype.isJVMStarted():
    jpype.startJVM()

from org.mpxj.mpp import MPPReader          # type: ignore
from org.mpxj import RelationType           # type: ignore

app = FastAPI(
    title="API Leitor MPP para Power BI",
    description="Lê arquivos .mpp via POST e expõe dados via GET para consumo no Power BI",
    version="2.1"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# MODELOS
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
# EXTRAÇÃO DE UMA TAREFA (função pura, testável)
# ------------------------------------------------------------------
def extrair_tarefa(task, project, nome_arquivo: str, id_projeto: str, data_proc: str) -> dict:
    nome_tarefa = safe_str(task.getName())

    # Duração
    duracao_obj = task.getDuration()
    duracao_valor = safe_float(duracao_obj.getDuration()) if duracao_obj else 0.0
    duracao_unidade = safe_str(duracao_obj.getUnits()) if duracao_obj else "Days"

    # Hierarquia
    parent_task = task.getParentTask()
    eh_tarefa_mae = safe_bool(task.getSummary())
    id_tarefa_mae = safe_int(parent_task.getID()) if parent_task else None
    nome_tarefa_mae = safe_str(parent_task.getName()) if parent_task and parent_task.getName() else None
    tipo_hierarquia = "mae" if eh_tarefa_mae else ("filha" if parent_task else "independente")

    # Recursos
    recursos = []
    try:
        for assignment in task.getResourceAssignments() or []:
            try:
                resource = assignment.getResource()
                if resource and resource.getName():
                    recursos.append(safe_str(resource.getName()))
            except Exception:
                continue
    except Exception:
        pass

    # Predecessoras
    predecessoras = []
    try:
        for relation in task.getPredecessors() or []:
            try:
                pred_task = None
                for metodo in ("getPredecessorTask", "getSourceTask", "getTask"):
                    if hasattr(relation, metodo):
                        pred_task = getattr(relation, metodo)()
                        if pred_task is not None:
                            break
                if pred_task:
                    predecessoras.append({
                        "id_tarefa": safe_int(pred_task.getID()),
                        "unique_id": safe_int(pred_task.getUniqueID()),
                        "tipo_vinculo": safe_str(relation.getType())
                    })
            except Exception:
                continue
    except Exception:
        pass

    # Sucessoras
    sucessoras = []
    try:
        for relation in task.getSuccessors() or []:
            try:
                suc_task = None
                for metodo in ("getSuccessorTask", "getTargetTask", "getTask"):
                    if hasattr(relation, metodo):
                        suc_task = getattr(relation, metodo)()
                        if suc_task is not None:
                            break
                if suc_task:
                    sucessoras.append({
                        "id_tarefa": safe_int(suc_task.getID()),
                        "unique_id": safe_int(suc_task.getUniqueID()),
                        "tipo_vinculo": safe_str(relation.getType())
                    })
            except Exception:
                continue
    except Exception:
        pass

    # Custos — com fallback
    custo_previsto = safe_float(task.getCost())
    try:
        custo_realizado = safe_float(task.getActualCost())
    except Exception:
        custo_realizado = 0.0
    try:
        custo_restante = safe_float(task.getRemainingCost())
    except Exception:
        custo_restante = 0.0

    # Trabalho (horas)
    trabalho_horas = 0.0
    try:
        w = task.getWork()
        if w:
            trabalho_horas = safe_float(w.getDuration())
    except Exception:
        pass

    trabalho_real = 0.0
    try:
        aw = task.getActualWork()
        if aw:
            trabalho_real = safe_float(aw.getDuration())
    except Exception:
        pass

    # Percentual (aceita inteiro 0-100 ou fração 0-1)
    perc = safe_float(task.getPercentageComplete())
    if 0 < perc <= 1:
        perc *= 100.0

    # Baseline
    baseline_inicio = None
    baseline_termino = None
    baseline_custo = 0.0
    try:
        b_inicio = task.getBaselineStart()
        baseline_inicio = safe_date(b_inicio)
        b_termino = task.getBaselineFinish()
        baseline_termino = safe_date(b_termino)
        b_custo = task.getBaselineCost()
        baseline_custo = safe_float(b_custo)
    except Exception:
        pass

    return {
        "id_projeto": id_projeto,
        "nome_arquivo": nome_arquivo,
        "unique_id": safe_int(task.getUniqueID()),
        "id": safe_int(task.getID()),
        "taskname": nome_tarefa,                       # NÃO filtra mais por nome vazio
        "inicio": safe_date(task.getStart()),
        "termino": safe_date(task.getFinish()),
        "inicio_real": safe_date(task.getActualStart()),
        "termino_real": safe_date(task.getActualFinish()),
        "percentual_concluido": perc,
        "duracao": duracao_valor,
        "unidade_duracao": duracao_unidade,
        "custo_previsto": custo_previsto,
        "custo_realizado": custo_realizado,
        "custo_restante": custo_restante,
        "custo": custo_previsto,
        "eh_marco": safe_bool(task.getMilestone()),
        "eh_critico": safe_bool(task.getCritical()),
        "eh_tarefa_mae": eh_tarefa_mae,
        "eh_tarefa_filha": parent_task is not None,
        "tipo_hierarquia": tipo_hierarquia,
        "id_tarefa_mae": id_tarefa_mae,
        "nome_tarefa_mae": nome_tarefa_mae,
        "recursos": recursos,
        "recursos_atribuidos": recursos,
        "predecessoras": predecessoras,
        "sucessoras": sucessoras,
        "trabalho_horas": trabalho_horas,
        "trabalho_real_horas": trabalho_real,
        "baseline_inicio": baseline_inicio,
        "baseline_termino": baseline_termino,
        "baseline_custo": baseline_custo,
        "notas": safe_str(task.getNotes()),
        "data_processamento": data_proc,
        # aliases para bater com os nomes do SharePoint
        "Tarefa": nome_tarefa,
        "Inicio": safe_date(task.getStart()),
        "Termino": safe_date(task.getFinish()),
    }

# ------------------------------------------------------------------
# 1. LEITURA (POST)
# ------------------------------------------------------------------
@app.post("/ler-mpp/")
async def ler_arquivo_mpp(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".mpp"):
        raise HTTPException(status_code=400, detail="Envie um arquivo com extensão .mpp válido.")

    temp_path = f"temp_{uuid.uuid4().hex}_{file.filename}"
    with open(temp_path, "wb") as buffer:
        buffer.write(await file.read())

    try:
        reader = MPPReader()
        # Alguns arquivos precisam dessas flags para leitura completa
        try:
            reader.setReadPassword("")           # tenta vazio primeiro
        except Exception:
            pass

        project = reader.read(temp_path)

        # ID do projeto estável (por nome de arquivo)
        id_projeto = str(abs(hash(file.filename)) % (10**8))
        data_proc = datetime.now().isoformat()

        tarefas = []
        ignoradas = 0
        erros = []

        for task in project.getTasks():
            # Pula APENAS a linha nula/raiz do MPP (id 0 e sem dados)
            try:
                tid = safe_int(task.getID(), -1)
                if tid == 0 and not safe_str(task.getName()) and not task.getStart():
                    ignoradas += 1
                    continue
            except Exception:
                pass

            try:
                tarefas.append(extrair_tarefa(task, project, file.filename, id_projeto, data_proc))
            except Exception as e:
                erros.append({"task_id": safe_int(task.getID(), -1), "erro": str(e)})

        db = carregar_dados()
        db["projetos"][file.filename] = {
            "id_projeto": id_projeto,
            "nome_arquivo": file.filename,
            "total_tarefas": len(tarefas),
            "tarefas_ignoradas": ignoradas,
            "erros_extracao": erros,
            "data_processamento": data_proc,
            "tarefas": tarefas
        }
        salvar_dados(db)

        return {
            "status": "sucesso",
            "id_projeto": id_projeto,
            "nome_arquivo": file.filename,
            "total_tarefas": len(tarefas),
            "tarefas_ignoradas": ignoradas,
            "total_erros": len(erros),
            "erros": erros[:10]      # mostra os 10 primeiros para debug
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo MPP: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ------------------------------------------------------------------
# 2. CONSUMO (GET)
# ------------------------------------------------------------------
@app.get("/tarefas/")
async def listar_todas_tarefas(
    nome_arquivo: Optional[str] = Query(None),
    eh_tarefa_mae: Optional[bool] = Query(None),
    eh_critico: Optional[bool] = Query(None),
    id_projeto: Optional[str] = Query(None)
):
    db = carregar_dados()
    todas_tarefas = []
    for projeto_nome, projeto_data in db.get("projetos", {}).items():
        for tarefa in projeto_data.get("tarefas", []):
            if nome_arquivo and tarefa.get("nome_arquivo") != nome_arquivo:
                continue
            if id_projeto and tarefa.get("id_projeto") != id_projeto:
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
    db = carregar_dados()
    resumo = []
    for nome, dados in db.get("projetos", {}).items():
        resumo.append({
            "id_projeto": dados.get("id_projeto"),
            "nome_arquivo": nome,
            "total_tarefas": dados.get("total_tarefas", 0),
            "tarefas_ignoradas": dados.get("tarefas_ignoradas", 0),
            "data_processamento": dados.get("data_processamento")
        })
    return {"total_projetos": len(resumo), "projetos": resumo}

@app.get("/projetos/{nome_arquivo}/tarefas/")
async def tarefas_por_projeto(nome_arquivo: str):
    db = carregar_dados()
    projeto = db.get("projetos", {}).get(nome_arquivo)
    if not projeto:
        raise HTTPException(status_code=404, detail=f"Projeto '{nome_arquivo}' não encontrado.")
    return {
        "nome_arquivo": nome_arquivo,
        "id_projeto": projeto.get("id_projeto"),
        "total_tarefas": projeto.get("total_tarefas", 0),
        "data_processamento": projeto.get("data_processamento"),
        "tarefas": projeto.get("tarefas", [])
    }

@app.get("/health/")
async def health_check():
    db = carregar_dados()
    return {
        "status": "online",
        "total_projetos": len(db.get("projetos", {})),
        "ultima_atualizacao": db.get("ultima_atualizacao")
    }

@app.delete("/projetos/{nome_arquivo}/")
async def deletar_projeto(nome_arquivo: str):
    db = carregar_dados()
    if nome_arquivo in db.get("projetos", {}):
        del db["projetos"][nome_arquivo]
        salvar_dados(db)
        return {"status": "deletado", "nome_arquivo": nome_arquivo}
    raise HTTPException(status_code=404, detail="Projeto não encontrado.")
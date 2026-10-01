import os
import json
import hashlib
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import jpype
import mpxj  # <-- ESSENCIAL: carrega o JAR do MPXJ no classpath

if not jpype.isJVMStarted():
    jpype.startJVM()

from org.mpxj.mpp import MPPReader  # type: ignore

# 1. Inicia a JVM
if not jpype.isJVMStarted():
    jpype.startJVM()

from org.mpxj.mpp import MPPReader  # type: ignore

app = FastAPI(
    title="API Central de Projetos MPP",
    description="Extrai TODOS os dados de arquivos .mpp: hierarquia, datas, custos, recursos, baseline e relacionamentos."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# PERSISTÊNCIA EM DISCO (sobrevive a restart)
# ------------------------------------------------------------------
ARQUIVO_DADOS = "dados_mpp.json"

def carregar_dados() -> Dict[str, dict]:
    if os.path.exists(ARQUIVO_DADOS):
        with open(ARQUIVO_DADOS, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def salvar_dados(db: Dict[str, dict]) -> None:
    with open(ARQUIVO_DADOS, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2, default=str)

DB_PROJETOS: Dict[str, dict] = carregar_dados()

# ------------------------------------------------------------------
# HELPERS (conversão Java → Python tratando null)
# ------------------------------------------------------------------
def safe_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        s = str(v).strip()
    except Exception:
        return default
    return default if s.lower() in ("null", "none", "nan") else s

def safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def safe_bool(v: Any) -> bool:
    try:
        return bool(v)
    except Exception:
        return False

def format_date(java_date: Any) -> Optional[str]:
    """Converte LocalDateTime/LocalDate/java.util.Date em ISO 8601."""
    if java_date is None:
        return None
    if hasattr(java_date, "toString"):
        try:
            s = java_date.toString()
            if s and s.lower() != "null":
                if "T" in s and len(s) == 16:      # 2024-01-01T08:00
                    s += ":00"
                return s
        except Exception:
            pass
    try:
        return java_date.isoformat()
    except Exception:
        return safe_str(java_date) or None

def gerar_id_projeto(nome_arquivo: str) -> str:
    """ID estável entre reinícios (MD5 do nome do arquivo)."""
    return hashlib.md5(nome_arquivo.encode("utf-8")).hexdigest()[:12]

# ------------------------------------------------------------------
# EXTRAÇÃO COMPLETA DE UMA TAREFA — 1 linha plana por tarefa
# ------------------------------------------------------------------
def extrair_tarefa(task, nome_arquivo: str, id_projeto: str, data_proc: str) -> dict:
    nome = safe_str(task.getName())

    # ----- Duração -----
    dur_obj = task.getDuration()
    duracao = safe_float(dur_obj.getDuration()) if dur_obj else 0.0
    unidade = safe_str(dur_obj.getUnits()) if dur_obj else "Days"

    # ----- Hierarquia -----
    parent = task.getParentTask()
    eh_mae = safe_bool(task.getSummary())
    eh_filha = parent is not None and safe_int(parent.getID(), 0) != 0

    id_pai = safe_int(parent.getID()) if (parent and safe_int(parent.getID(), 0) != 0) else None
    nome_pai = safe_str(parent.getName()) if (parent and safe_int(parent.getID(), 0) != 0) else None
    uid_pai = safe_int(parent.getUniqueID()) if (parent and safe_int(parent.getID(), 0) != 0) else None

    if eh_mae:
        tipo_hier = "mae"
    elif eh_filha:
        tipo_hier = "filha"
    else:
        tipo_hier = "independente"

    # Nível WBS (outline level)
    nivel = safe_int(task.getOutlineLevel(), 0) or 0

    # ----- Recursos -----
    recursos = []
    try:
        for a in task.getResourceAssignments() or []:
            try:
                r = a.getResource()
                if r and r.getName():
                    recursos.append(safe_str(r.getName()))
            except Exception:
                continue
    except Exception:
        pass
    recursos_str = "; ".join(recursos)

    # ----- Predecessoras -----
    predecessoras = []
    try:
        for rel in task.getPredecessors() or []:
            try:
                pred = None
                for m in ("getPredecessorTask", "getSourceTask", "getTask"):
                    if hasattr(rel, m):
                        pred = getattr(rel, m)()
                        if pred is not None:
                            break
                if pred:
                    predecessoras.append({
                        "id": safe_int(pred.getID()),
                        "unique_id": safe_int(pred.getUniqueID()),
                        "nome": safe_str(pred.getName()),
                        "tipo": safe_str(rel.getType()),
                    })
            except Exception:
                continue
    except Exception:
        pass

    # ----- Sucessoras -----
    sucessoras = []
    try:
        for rel in task.getSuccessors() or []:
            try:
                suc = None
                for m in ("getSuccessorTask", "getTargetTask", "getTask"):
                    if hasattr(rel, m):
                        suc = getattr(rel, m)()
                        if suc is not None:
                            break
                if suc:
                    sucessoras.append({
                        "id": safe_int(suc.getID()),
                        "unique_id": safe_int(suc.getUniqueID()),
                        "nome": safe_str(suc.getName()),
                        "tipo": safe_str(rel.getType()),
                    })
            except Exception:
                continue
    except Exception:
        pass

    # ----- Custos -----
    custo_previsto  = safe_float(task.getCost())
    custo_realizado = safe_float(task.getActualCost())
    custo_restante  = safe_float(task.getRemainingCost())
    baseline_custo  = safe_float(task.getBaselineCost())

    # ----- Trabalho -----
    trabalho = 0.0
    try:
        w = task.getWork()
        if w: trabalho = safe_float(w.getDuration())
    except Exception:
        pass
    trabalho_real = 0.0
    try:
        aw = task.getActualWork()
        if aw: trabalho_real = safe_float(aw.getDuration())
    except Exception:
        pass

    # ----- Percentual -----
    perc = safe_float(task.getPercentageComplete())
    if 0 < perc <= 1:
        perc *= 100.0

    # ----- Datas -----
    inicio     = format_date(task.getStart())
    termino    = format_date(task.getFinish())
    inicio_r   = format_date(task.getActualStart())
    termino_r  = format_date(task.getActualFinish())
    baseline_i = format_date(task.getBaselineStart())
    baseline_t = format_date(task.getBaselineFinish())

    # ----- WBS / Notas -----
    wbs   = safe_str(task.getWBS())
    notas = safe_str(task.getNotes())

    # ==============================================================
    # SAÍDA PLANA — 21 campos do SharePoint + extras
    # ==============================================================
    return {
        # ---- colunas exatas do SharePoint ----
        "ID Projeto":          id_projeto,
        "NomeArquivo":         nome_arquivo,
        "UniqueID":            safe_int(task.getUniqueID()),
        "Tarefa":              nome,
        "Inicio":              inicio,
        "Termino":             termino,
        "InicioReal":          inicio_r,
        "TerminoReal":         termino_r,
        "PercentualConcluido": perc,
        "Duracao":             duracao,
        "UnidadeDuracao":      unidade,
        "CustoPrevisto":       custo_previsto,
        "CustoRealizado":      custo_realizado,
        "EhMarco":             safe_bool(task.getMilestone()),
        "EhCritico":           safe_bool(task.getCritical()),
        "TipoHierarquia":      tipo_hier,
        "IdTarefaMae":         id_pai,
        "NomeTarefaMae":       nome_pai,
        "Recursos":            recursos_str,
        "Notas":               notas,
        "DataProcessamento":   data_proc,

        # ---- extras úteis no Power BI ----
        "id":                  safe_int(task.getID()),
        "wbs":                 wbs,
        "nivel_estrutura":     nivel,
        "eh_tarefa_mae":       eh_mae,
        "eh_tarefa_filha":     eh_filha,
        "unique_id_mae":       uid_pai,
        "custo_restante":      custo_restante,
        "trabalho_horas":      trabalho,
        "trabalho_real_horas": trabalho_real,
        "baseline_inicio":     baseline_i,
        "baseline_termino":    baseline_t,
        "baseline_custo":      baseline_custo,
        "predecessoras":       predecessoras,
        "sucessoras":          sucessoras,
    }

# ------------------------------------------------------------------
# PARSER PRINCIPAL DO MPP
# ------------------------------------------------------------------
def parse_mpp_file(file_path: str, filename: str) -> dict:
    reader = MPPReader()
    project = reader.read(file_path)

    properties = project.getProjectProperties()
    id_projeto = gerar_id_projeto(filename)
    data_proc = datetime.now().isoformat()

    tarefas = []
    ignoradas = 0
    erros = []

    for task in project.getTasks():
        try:
            # Pula apenas a linha raiz (ID 0 sem dados)
            tid = safe_int(task.getID(), -1)
            if tid == 0 and not safe_str(task.getName()) and not task.getStart():
                ignoradas += 1
                continue
        except Exception:
            pass

        try:
            tarefas.append(extrair_tarefa(task, filename, id_projeto, data_proc))
        except Exception as e:
            erros.append({"task_id": safe_int(task.getID(), -1), "erro": str(e)})

    pct_proj = properties.getPercentageComplete()
    pct_proj_val = safe_float(pct_proj.doubleValue()) if pct_proj is not None else 0.0

    return {
        "id_projeto": id_projeto,
        "nome_arquivo": filename,
        "titulo_projeto": safe_str(properties.getProjectTitle()) or filename,
        "data_inicio_projeto": format_date(properties.getStartDate()),
        "data_fim_projeto": format_date(properties.getFinishDate()),
        "percentual_concluido_total": pct_proj_val,
        "total_tarefas": len(tarefas),
        "tarefas_ignoradas": ignoradas,
        "erros_extracao": erros,
        "data_processamento": data_proc,
        "tarefas": tarefas,
    }

# ------------------------------------------------------------------
# POST /projetos/upload
# ------------------------------------------------------------------
@app.post("/projetos/upload", summary="Processa .mpp e salva todos os dados")
async def upload_projeto(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".mpp", ".xml")):
        raise HTTPException(status_code=400, detail="Apenas arquivos .mpp e .xml são aceitos.")

    temp_path = f"/tmp/{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            buffer.write(await file.read())

        dados_projeto = parse_mpp_file(temp_path, file.filename)
        DB_PROJETOS[file.filename] = dados_projeto
        salvar_dados(DB_PROJETOS)

        return {
            "status": "sucesso",
            "id_projeto": dados_projeto["id_projeto"],
            "nome_arquivo": file.filename,
            "total_tarefas_extraidas": dados_projeto["total_tarefas"],
            "tarefas_ignoradas": dados_projeto["tarefas_ignoradas"],
            "total_erros": len(dados_projeto["erros_extracao"]),
            "erros": dados_projeto["erros_extracao"][:10],
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao processar o arquivo MPP: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ------------------------------------------------------------------
# GET /projetos/central — visão completa
# ------------------------------------------------------------------
@app.get("/projetos/central", summary="Central com todos os projetos e tarefas")
def obter_central_de_projetos():
    return {
        "quantidade_projetos": len(DB_PROJETOS),
        "projetos": list(DB_PROJETOS.values()),
    }

# ------------------------------------------------------------------
# GET /projetos — resumo (leve, só metadados)
# ------------------------------------------------------------------
@app.get("/projetos", summary="Lista resumida dos projetos")
def listar_projetos():
    resumo = [
        {
            "id_projeto": p.get("id_projeto"),
            "nome_arquivo": p.get("nome_arquivo"),
            "titulo_projeto": p.get("titulo_projeto"),
            "total_tarefas": p.get("total_tarefas"),
            "data_processamento": p.get("data_processamento"),
        }
        for p in DB_PROJETOS.values()
    ]
    return {"total_projetos": len(resumo), "projetos": resumo}

# ------------------------------------------------------------------
# GET /tarefas — tabela plana (ideal para Power BI)
# ------------------------------------------------------------------
@app.get("/tarefas", summary="Todas as tarefas de todos os projetos (tabela plana)")
def listar_tarefas(
    nome_arquivo: Optional[str] = Query(None),
    id_projeto: Optional[str] = Query(None),
    eh_tarefa_mae: Optional[bool] = Query(None),
    eh_critico: Optional[bool] = Query(None),
):
    linhas = []
    for proj in DB_PROJETOS.values():
        if nome_arquivo and proj.get("nome_arquivo") != nome_arquivo:
            continue
        if id_projeto and str(proj.get("id_projeto")) != str(id_projeto):
            continue
        for t in proj.get("tarefas", []):
            if eh_tarefa_mae is not None and t.get("eh_tarefa_mae") != eh_tarefa_mae:
                continue
            if eh_critico is not None and t.get("EhCritico") != eh_critico:
                continue
            linhas.append(t)
    return {"total_registros": len(linhas), "tarefas": linhas}

# ------------------------------------------------------------------
# GET /tarefas-sharepoint — só as 21 colunas exatas
# ------------------------------------------------------------------
COLUNAS_SHAREPOINT = [
    "ID Projeto", "NomeArquivo", "UniqueID", "Tarefa", "Inicio", "Termino",
    "InicioReal", "TerminoReal", "PercentualConcluido", "Duracao",
    "UnidadeDuracao", "CustoPrevisto", "CustoRealizado", "EhMarco",
    "EhCritico", "TipoHierarquia", "IdTarefaMae", "NomeTarefaMae",
    "Recursos", "Notas", "DataProcessamento",
]

@app.get("/tarefas-sharepoint", summary="Tarefas no formato exato do SharePoint")
def tarefas_sharepoint(nome_arquivo: Optional[str] = Query(None)):
    linhas = []
    for proj in DB_PROJETOS.values():
        if nome_arquivo and proj.get("nome_arquivo") != nome_arquivo:
            continue
        for t in proj.get("tarefas", []):
            linhas.append({col: t.get(col) for col in COLUNAS_SHAREPOINT})
    return {"total": len(linhas), "colunas": COLUNAS_SHAREPOINT, "linhas": linhas}

# ------------------------------------------------------------------
# GET /health
# ------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "online", "total_projetos": len(DB_PROJETOS)}

# ------------------------------------------------------------------
# DELETE /projetos/{nome_arquivo}
# ------------------------------------------------------------------
@app.delete("/projetos/{nome_arquivo}", summary="Remove um projeto da base")
def deletar_projeto(nome_arquivo: str):
    if nome_arquivo in DB_PROJETOS:
        del DB_PROJETOS[nome_arquivo]
        salvar_dados(DB_PROJETOS)
        return {"status": "deletado", "nome_arquivo": nome_arquivo}
    raise HTTPException(status_code=404, detail="Projeto não encontrado.")

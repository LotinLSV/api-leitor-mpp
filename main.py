# main.py
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import jpype
import json
import os
import uuid
import io
import csv
import base64
import hashlib
from typing import List, Optional, Any
from pydantic import BaseModel
from datetime import datetime

import pandas as pd

# ------------------------------------------------------------------
# CONFIGURAÇÃO
# ------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "troque-esta-chave-em-producao")
ARQUIVO_DADOS = "dados_mpp.json"


# ------------------------------------------------------------------
# AUTENTICAÇÃO SIMPLES VIA HEADER
# ------------------------------------------------------------------
async def verificar_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API Key inválida")
    return True


# ------------------------------------------------------------------
# HELPERS DE ID
# ------------------------------------------------------------------
def gerar_id_projeto(nome_arquivo: str) -> str:
    return hashlib.md5(nome_arquivo.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------
# PERSISTÊNCIA
# ------------------------------------------------------------------
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
# HELPERS DE CONVERSÃO JAVA -> PYTHON
# ------------------------------------------------------------------
def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        s = str(value).strip()
    except Exception:
        return default
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
    if value is None:
        return None
    if hasattr(value, "toString"):
        try:
            s = value.toString()
            if s and s.lower() != "null":
                if "T" in s and len(s) == 16:
                    s += ":00"
                return s
        except Exception:
            pass
    try:
        return value.isoformat()
    except Exception:
        return safe_str(value) or None


# ------------------------------------------------------------------
# INICIA JVM (MPXJ)
# ------------------------------------------------------------------
# if not jpype.isJVMStarted():
#     jpype.startJVM(classpath=[
#         "/app/libs/mpxj.jar",
#         "/app/libs/mpxj-deps.jar",
#     ])

if not jpype.isJVMStarted():
    jpype.startJVM(
        "-Xms64m",
        "-Xmx320m",
        "-XX:+UseSerialGC",
        classpath=["/app/libs/mpxj.jar"],
    )


# if not jpype.isJVMStarted():
#     jpype.startJVM()

from org.mpxj.mpp import MPPReader          # type: ignore
from org.mpxj import RelationType           # type: ignore


# ------------------------------------------------------------------
# APP
# ------------------------------------------------------------------
app = FastAPI(
    title="API Leitor MPP para Power BI",
    description="Lê arquivos .mpp, expõe dados em JSON e exporta CSV/XLSX para Power BI",
    version="3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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


class ArquivoBase64(BaseModel):
    filename: str
    content_base64: str


# ------------------------------------------------------------------
# EXTRAÇÃO COMPLETA DE UMA TAREFA (plana, 1 linha por tarefa)
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
    eh_tarefa_filha = parent_task is not None
    id_tarefa_mae = safe_int(parent_task.getID()) if parent_task else None
    unique_id_mae = safe_int(parent_task.getUniqueID()) if parent_task else None
    nome_tarefa_mae = safe_str(parent_task.getName()) if parent_task and parent_task.getName() else None

    if eh_tarefa_mae:
        tipo_hierarquia = "mae"
    elif eh_tarefa_filha:
        tipo_hierarquia = "filha"
    else:
        tipo_hierarquia = "independente"

    nivel = 0
    p = parent_task
    while p is not None:
        nivel += 1
        p = p.getParentTask()

    # Recursos
    recursos = []
    try:
        for assignment in task.getResourceAssignments() or []:
            try:
                r = assignment.getResource()
                if r and r.getName():
                    recursos.append(safe_str(r.getName()))
            except Exception:
                continue
    except Exception:
        pass
    recursos_str = "; ".join(recursos)

    # Predecessoras
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
                        "tipo": safe_str(rel.getType()),
                    })
            except Exception:
                continue
    except Exception:
        pass

    # Sucessoras
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
                        "tipo": safe_str(rel.getType()),
                    })
            except Exception:
                continue
    except Exception:
        pass

    # Custos
    custo_previsto = safe_float(task.getCost())
    custo_realizado = safe_float(task.getActualCost())
    custo_restante = safe_float(task.getRemainingCost())

    # Trabalho
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

    # Percentual
    perc = safe_float(task.getPercentageComplete())
    if 0 < perc <= 1:
        perc *= 100.0

    # Baseline
    baseline_inicio = safe_date(task.getBaselineStart())
    baseline_termino = safe_date(task.getBaselineFinish())
    baseline_custo = safe_float(task.getBaselineCost())

    # Datas
    inicio = safe_date(task.getStart())
    termino = safe_date(task.getFinish())
    ini_real = safe_date(task.getActualStart())
    fim_real = safe_date(task.getActualFinish())

    # Texto
    wbs = safe_str(task.getWBS())
    notas = safe_str(task.getNotes())

    return {
        # Chaves exatas para Excel / SharePoint / Power BI
        "ID Projeto": id_projeto,
        "NomeArquivo": nome_arquivo,
        "UniqueID": safe_int(task.getUniqueID()),
        "Tarefa": nome_tarefa,
        "Inicio": inicio,
        "Termino": termino,
        "InicioReal": ini_real,
        "TerminoReal": fim_real,
        "PercentualConcluido": perc,
        "Duracao": duracao_valor,
        "UnidadeDuracao": duracao_unidade,
        "CustoPrevisto": custo_previsto,
        "CustoRealizado": custo_realizado,
        "EhMarco": safe_bool(task.getMilestone()),
        "EhCritico": safe_bool(task.getCritical()),
        "TipoHierarquia": tipo_hierarquia,
        "IdTarefaMae": id_tarefa_mae,
        "NomeTarefaMae": nome_tarefa_mae,
        "Recursos": recursos_str,
        "Notas": notas,
        "DataProcessamento": data_proc,

        # Extras úteis para Power BI
        "id": safe_int(task.getID()),
        "wbs": wbs,
        "nivel_estrutura": nivel,
        "eh_tarefa_mae": eh_tarefa_mae,
        "eh_tarefa_filha": eh_tarefa_filha,
        "unique_id_mae": unique_id_mae,
        "custo_restante": custo_restante,
        "trabalho_horas": trabalho_horas,
        "trabalho_real_horas": trabalho_real,
        "baseline_inicio": baseline_inicio,
        "baseline_termino": baseline_termino,
        "baseline_custo": baseline_custo,
        "predecessoras": predecessoras,
        "sucessoras": sucessoras,
    }


# ------------------------------------------------------------------
# FUNÇÃO REUTILIZÁVEL: LER MPP DE UM PATH
# ------------------------------------------------------------------
def processar_mpp(temp_path: str, nome_arquivo: str) -> dict:
    reader = MPPReader()
    try:
        reader.setReadPassword("")
    except Exception:
        pass

    project = reader.read(temp_path)
    id_projeto = gerar_id_projeto(nome_arquivo)
    data_proc = datetime.now().isoformat()

    tarefas = []
    ignoradas = 0
    erros = []

    for task in project.getTasks():
        try:
            tid = safe_int(task.getID(), -1)
            if tid == 0 and not safe_str(task.getName()) and not task.getStart():
                ignoradas += 1
                continue
        except Exception:
            pass

        try:
            tarefas.append(extrair_tarefa(task, project, nome_arquivo, id_projeto, data_proc))
        except Exception as e:
            erros.append({"task_id": safe_int(task.getID(), -1), "erro": str(e)})

    db = carregar_dados()
    db["projetos"][nome_arquivo] = {
        "id_projeto": id_projeto,
        "nome_arquivo": nome_arquivo,
        "total_tarefas": len(tarefas),
        "tarefas_ignoradas": ignoradas,
        "erros_extracao": erros,
        "data_processamento": data_proc,
        "tarefas": tarefas,
    }
    salvar_dados(db)

    return {
        "status": "sucesso",
        "id_projeto": id_projeto,
        "nome_arquivo": nome_arquivo,
        "total_tarefas": len(tarefas),
        "tarefas_ignoradas": ignoradas,
        "total_erros": len(erros),
        "erros": erros[:10],
    }


# ------------------------------------------------------------------
# COLUNAS PADRÃO PARA EXPORTAÇÃO
# ------------------------------------------------------------------
COLUNAS_SHAREPOINT = [
    "ID Projeto", "NomeArquivo", "UniqueID", "Tarefa", "Inicio", "Termino",
    "InicioReal", "TerminoReal", "PercentualConcluido", "Duracao",
    "UnidadeDuracao", "CustoPrevisto", "CustoRealizado", "EhMarco",
    "EhCritico", "TipoHierarquia", "IdTarefaMae", "NomeTarefaMae",
    "Recursos", "Notas", "DataProcessamento",
]


# ------------------------------------------------------------------
# 1. LEITURA (POST multipart)
# ------------------------------------------------------------------
@app.post("/ler-mpp/")
async def ler_arquivo_mpp(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".mpp"):
        raise HTTPException(status_code=400, detail="Envie um arquivo com extensão .mpp válido.")

    temp_path = f"temp_{uuid.uuid4().hex}_{file.filename}"
    with open(temp_path, "wb") as buffer:
        buffer.write(await file.read())

    try:
        return processar_mpp(temp_path, file.filename)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo MPP: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ------------------------------------------------------------------
# 2. LEITURA (POST Base64 — recomendado para Power Automate)
# ------------------------------------------------------------------
@app.post("/ler-mpp-base64/")
async def ler_mpp_base64(payload: ArquivoBase64):
    if not payload.filename.lower().endswith(".mpp"):
        raise HTTPException(status_code=400, detail="Arquivo precisa ser .mpp")

    temp_path = f"temp_{uuid.uuid4().hex}_{payload.filename}"
    try:
        with open(temp_path, "wb") as f:
            f.write(base64.b64decode(payload.content_base64))

        return processar_mpp(temp_path, payload.filename)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo MPP: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ------------------------------------------------------------------
# 3. CONSUMO (GET) — JSON completo
# ------------------------------------------------------------------
@app.get("/tarefas/")
async def listar_todas_tarefas(
    nome_arquivo: Optional[str] = Query(None),
    eh_tarefa_mae: Optional[bool] = Query(None),
    eh_critico: Optional[bool] = Query(None),
    id_projeto: Optional[str] = Query(None),
):
    db = carregar_dados()
    todas_tarefas = []
    for projeto_nome, projeto_data in db.get("projetos", {}).items():
        for t in projeto_data.get("tarefas", []):
            if nome_arquivo and t.get("NomeArquivo") != nome_arquivo:
                continue
            if id_projeto and str(t.get("ID Projeto")) != str(id_projeto):
                continue
            if eh_tarefa_mae is not None and t.get("eh_tarefa_mae") != eh_tarefa_mae:
                continue
            if eh_critico is not None and t.get("EhCritico") != eh_critico:
                continue
            todas_tarefas.append(t)
    return {
        "total_registros": len(todas_tarefas),
        "ultima_atualizacao": db.get("ultima_atualizacao"),
        "tarefas": todas_tarefas,
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
            "data_processamento": dados.get("data_processamento"),
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
        "tarefas": projeto.get("tarefas", []),
    }


@app.get("/health/")
async def health_check():
    db = carregar_dados()
    return {
        "status": "online",
        "total_projetos": len(db.get("projetos", {})),
        "ultima_atualizacao": db.get("ultima_atualizacao"),
    }


@app.delete("/projetos/{nome_arquivo}/")
async def deletar_projeto(nome_arquivo: str):
    db = carregar_dados()
    if nome_arquivo in db.get("projetos", {}):
        del db["projetos"][nome_arquivo]
        salvar_dados(db)
        return {"status": "deletado", "nome_arquivo": nome_arquivo}
    raise HTTPException(status_code=404, detail="Projeto não encontrado.")


# ------------------------------------------------------------------
# 4. FORMATO PLANO (para Power BI / Excel / SharePoint)
# ------------------------------------------------------------------
@app.get("/tarefas-sharepoint/")
async def tarefas_sharepoint(nome_arquivo: Optional[str] = Query(None)):
    db = carregar_dados()
    linhas = []
    for proj_nome, proj in db.get("projetos", {}).items():
        if nome_arquivo and proj_nome != nome_arquivo:
            continue
        for t in proj.get("tarefas", []):
            linhas.append({col: t.get(col) for col in COLUNAS_SHAREPOINT})
    return {"total": len(linhas), "colunas": COLUNAS_SHAREPOINT, "linhas": linhas}


# ------------------------------------------------------------------
# 5. EXPORT CSV (leve e rápido)
# ------------------------------------------------------------------
@app.get("/export/csv/", dependencies=[Depends(verificar_api_key)])
async def export_csv(nome_arquivo: Optional[str] = Query(None)):
    db = carregar_dados()
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=COLUNAS_SHAREPOINT,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()

    for proj in db.get("projetos", {}).values():
        if nome_arquivo and proj.get("nome_arquivo") != nome_arquivo:
            continue
        for t in proj.get("tarefas", []):
            writer.writerow({col: t.get(col, "") for col in COLUNAS_SHAREPOINT})

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=cronogramas.csv"},
    )


# ------------------------------------------------------------------
# 6. EXPORT XLSX (Excel real, com tipos)
# ------------------------------------------------------------------
@app.get("/export/xlsx/", dependencies=[Depends(verificar_api_key)])
async def export_xlsx(nome_arquivo: Optional[str] = Query(None)):
    db = carregar_dados()
    linhas = []
    for proj in db.get("projetos", {}).values():
        if nome_arquivo and proj.get("nome_arquivo") != nome_arquivo:
            continue
        for t in proj.get("tarefas", []):
            linhas.append({col: t.get(col) for col in COLUNAS_SHAREPOINT})

    df = pd.DataFrame(linhas, columns=COLUNAS_SHAREPOINT)

    # Converte colunas de data para datetime real
    for col in ("Inicio", "Termino", "InicioReal", "TerminoReal", "DataProcessamento"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Numéricos
    for col in ("PercentualConcluido", "Duracao", "CustoPrevisto", "CustoRealizado", "UniqueID", "IdTarefaMae"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Cronogramas")
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=cronogramas.xlsx"},
    )

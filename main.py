import os
import json
from typing import List, Optional, Dict
from fastapi import FastAPI, UploadFile, File, HTTPException
import jpype
import mpxj

# 1. Inicia a JVM se ainda não estiver rodando
if not jpype.isJVMStarted():
    jpype.startJVM()

# 2. Imports das classes Java do MPXJ
from org.mpxj.mpp import MPPReader  # type: ignore

app = FastAPI(
    title="API Central de Projetos MPP", 
    description="Extrai dados detalhados de arquivos .mpp incluindo hierarquia, dependências e baseline."
)

# Banco de dados temporário em memória
DB_PROJETOS: Dict[str, dict] = {}


def format_date(java_date) -> Optional[str]:
    """Auxiliar para converter objetos de data do Java/MPXJ para string ISO"""
    if java_date is None:
        return None
    return str(java_date)


def parse_mpp_file(file_path: str, filename: str) -> dict:
    reader = MPPReader()
    project = reader.read(file_path)
    
    properties = project.getProjectProperties()
    
    lista_tarefas = []
    
    # Percorre todas as tarefas do projeto
    for task in project.getTasks():
        # Ignora tarefas nulas/vazias ou a tarefa raiz (ID 0)
        if task is None or not task.getName() or task.getID().intValue() == 0:
            continue

        # --- 1. HIERARQUIA (Mãe / Filha / Nível) ---
        outline_level = task.getOutlineLevel().intValue() if task.getOutlineLevel() else 0
        is_summary = task.getSummary()  # True se for tarefa Mãe (Resumo)
        
        parent_task = task.getParentTask()
        parent_id = parent_task.getID().intValue() if (parent_task and parent_task.getID().intValue() != 0) else None
        parent_name = str(parent_task.getName()) if (parent_task and parent_task.getID().intValue() != 0) else None

        # --- 2. PREDECESSORAS E SUCESSORAS (CORRIGIDO) ---
        predecessoras = []
        if task.getPredecessors():
            for rel in task.getPredecessors():
                pred_task = rel.getPredecessorTask()
                if pred_task:
                    predecessoras.append({
                        "task_id": pred_task.getID().intValue(),
                        "task_name": str(pred_task.getName()),
                        "tipo_relacao": str(rel.getType())
                    })

        sucessoras = []
        if task.getSuccessors():
            for rel in task.getSuccessors():
                succ_task = rel.getSuccessorTask()
                if succ_task:
                    sucessoras.append({
                        "task_id": succ_task.getID().intValue(),
                        "task_name": str(succ_task.getName()),
                        "tipo_relacao": str(rel.getType())
                    })

        # Porcentagem de conclusão da tarefa
        pct_task = task.getPercentageComplete()
        pct_task_value = float(pct_task.doubleValue()) if pct_task is not None else 0.0

        # --- 3. MONTAGEM DO DICIONÁRIO DA TAREFA ---
        tarefa_dict = {
            "id": task.getID().intValue(),
            "wbs": str(task.getWBS()) if task.getWBS() else "",
            "nome": str(task.getName()),
            "percentual_concluido": pct_task_value,
            
            # Estrutura Hierárquica
            "hierarquia": {
                "nivel_estrutura": outline_level,
                "e_tarefa_mae": bool(is_summary),
                "e_tarefa_filha": not bool(is_summary) and outline_level > 1,
                "id_tarefa_pai": parent_id,
                "nome_tarefa_pai": parent_name
            },

            # Datas Planejadas / Atuais
            "datas": {
                "inicio_planejado": format_date(task.getStart()),
                "termino_planejado": format_date(task.getFinish()),
                # Início e Término Real (Executado)
                "inicio_real": format_date(task.getActualStart()),
                "termino_real": format_date(task.getActualFinish()),
                # Linha de Base 0 (Baseline Principal)
                "baseline_inicio": format_date(task.getBaselineStart()),
                "baseline_termino": format_date(task.getBaselineFinish())
            },

            # Relacionamentos
            "predecessoras": predecessoras,
            "sucessoras": sucessoras
        }

        lista_tarefas.append(tarefa_dict)

    # Porcentagem total do projeto
    pct_proj = properties.getPercentageComplete()
    pct_proj_value = float(pct_proj.doubleValue()) if pct_proj is not None else 0.0

    return {
        "nome_arquivo": filename,
        "titulo_projeto": str(properties.getProjectTitle() or filename),
        "data_inicio_projeto": format_date(properties.getStartDate()),
        "data_fim_projeto": format_date(properties.getFinishDate()),
        "percentual_concluido_total": pct_proj_value,
        "total_tarefas": len(lista_tarefas),
        "tarefas": lista_tarefas
    }


@app.post("/projetos/upload", summary="Processa o arquivo .mpp e salva os dados detalhados")
async def upload_projeto(file: UploadFile = File(...)):
    if not file.filename.endswith(('.mpp', '.xml')):
        raise HTTPException(status_code=400, detail="Apenas arquivos .mpp e .xml são aceitos.")

    temp_path = f"/tmp/{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            buffer.write(await file.read())
        
        dados_projeto = parse_mpp_file(temp_path, file.filename)
        
        # Salva ou atualiza no dicionário centralizador pelo nome do arquivo
        DB_PROJETOS[file.filename] = dados_projeto
        
        return {
            "status": "sucesso", 
            "mensagem": f"Projeto '{file.filename}' processado com sucesso.",
            "total_tarefas_extraidas": dados_projeto["total_tarefas"]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar o arquivo MPP: {str(e)}")
        
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.get("/projetos/central", summary="Retorna a Central com todos os Projetos e suas Tarefas")
def obter_central_de_projetos():
    return {
        "quantidade_projetos": len(DB_PROJETOS),
        "projetos": list(DB_PROJETOS.values())
    }
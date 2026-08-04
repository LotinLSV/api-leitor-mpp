from fastapi import FastAPI, UploadFile, File, HTTPException
import jpype
import mpxj
import json

# Inicia a Máquina Virtual Java necessária para o MPXJ rodar no Python
if not jpype.isJVMStarted():
    jpype.startJVM()

# Importação dinâmica com ignore para o Pylance/VS Code não acusar erro visual
from org.mpxj.mpp import MPPReader # type: ignore

app = FastAPI(
    title="API Leitor MPP", 
    description="Lê arquivos do Microsoft Project (.mpp) e retorna JSON estruturado para o Power Automate"
)

@app.post("/ler-mpp/")
async def ler_arquivo_mpp(file: UploadFile = File(...)):
    # Valida se a extensão do arquivo enviado é .mpp
    if not file.filename.endswith(".mpp"):
        raise HTTPException(status_code=400, detail="Envie um arquivo com extensão .mpp válido.")
    
    # Salva o arquivo temporariamente no disco para leitura da biblioteca
    temp_path = f"temp_{file.filename}"
    with open(temp_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
        
    try:
        # Inicializa o leitor e faz o parse do projeto
        reader = MPPReader()
        project = reader.read(temp_path)
        
        tarefas = []
        for task in project.getTasks():
            # Converte o retorno Java para string Python nativa
            nome_tarefa = str(task.getName()).strip() if task.getName() is not None else ""
            
            # Filtra linhas completamente em branco do Project
            if nome_tarefa:
                
                # Tratamento seguro da Duração
                duracao = task.getDuration()
                duracao_valor = duracao.getDuration() if duracao else 0
                duracao_unidade = str(duracao.getUnits()) if duracao else "Days"

                # Mapeamento de Recursos Atribuídos
                recursos = []
                for assignment in task.getResourceAssignments():
                    resource = assignment.getResource()
                    if resource and resource.getName():
                        recursos.append(str(resource.getName()))

                # Mapeamento Atualizado e Seguro de Predecessoras
                predecessoras = []
                if task.getPredecessors():
                    for relation in task.getPredecessors():
                        # Utiliza a API moderna do MPXJ com fallback para versões antigas
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

                # Mapeamento Atualizado e Seguro de Sucessoras
                sucessoras = []
                if task.getSuccessors():
                    for relation in task.getSuccessors():
                        # Utiliza a API moderna do MPXJ com fallback para versões antigas
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

                # Monta a estrutura JSON final de cada tarefa
                tarefas.append({
                    "id": task.getID(),
                    "unique_id": task.getUniqueID(),
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
                    "eh_critico": bool(task.getCritical()),
                    "recursos_atribuidos": recursos,
                    "predecessoras": predecessoras,
                    "sucessoras": sucessoras,
                    "notas": str(task.getNotes()) if task.getNotes() else ""
                })
                
        dados_projeto = {
            "nome_projeto": project.getProjectProperties().getName() if project.getProjectProperties() and project.getProjectProperties().getName() else "Projeto sem nome",
            "nome_arquivo": file.filename,
            "total_tarefas": len(tarefas),
            "tarefas": tarefas
        }
        
        return dados_projeto

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo MPP: {str(e)}")
    
    finally:
        # Garante a exclusão do arquivo temporário após o processamento
        import os
        if os.path.exists(temp_path):
            os.remove(temp_path)

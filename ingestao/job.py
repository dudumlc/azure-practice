"""
O "job" é o registro da última verificação: um único documento (id "job", partição "_meta") no Cosmos.

A API do Static Web App (marco seguinte) cria o job como "na_fila" e põe uma mensagem na fila; a Function
executa executar_job(), que o leva a "rodando" e depois ao estado final. A tela só lê esse documento.
A espera entre verificações vem pronta no próprio job (proxima_verificacao_em), calculada aqui ao terminar.

    estado:  na_fila | rodando | ok | sem_novidade | bloqueio | erro
"""
from datetime import datetime, timedelta, timezone

from atualizar import atualizar, tentar

JOB_ID = "job"
JOB_PARTICAO = "_meta"
ESPERA_COM_NOVIDADE = timedelta(days=7)      # saiu edição nova: o próximo informativo só em uma semana
ESPERA_SEM_NOVIDADE = timedelta(hours=1)     # nada novo, bloqueio ou erro: pode tentar de novo logo
TRAVA_RODANDO = timedelta(minutes=15)        # um job "rodando" há mais que isso é considerado travado
INTERVALO_PROGRESSO = 3.0                    # segundos entre gravações da etapa, para não gastar RU à toa


def agora():
    return datetime.now(timezone.utc)


def iso(momento):
    return momento.isoformat(timespec="seconds")


def ler_job(container):
    try:
        return tentar(container.read_item, JOB_ID, partition_key=JOB_PARTICAO)
    except Exception as erro:
        if "NotFound" in type(erro).__name__:
            return None
        raise


def executar_job(container, rodar=atualizar, relogio=agora):
    """Roda a atualização e registra o andamento no job. Devolve o job final."""
    job = ler_job(container) or {"id": JOB_ID, "edicao": JOB_PARTICAO}
    if job.get("estado") == "rodando" and job.get("iniciado_em"):
        if relogio() - datetime.fromisoformat(job["iniciado_em"]) < TRAVA_RODANDO:
            return job                                       # mensagem repetida: já tem uma execução em andamento
    job.update(estado="rodando", iniciado_em=iso(relogio()), finalizado_em=None, etapa="Começando…",
               mensagem="", resultado=None)
    tentar(container.upsert_item, job)

    ultima = [relogio()]

    def progresso(texto):
        job["etapa"] = texto
        if (relogio() - ultima[0]).total_seconds() >= INTERVALO_PROGRESSO:
            ultima[0] = relogio()
            tentar(container.upsert_item, job)

    try:
        resultado = rodar(container, progresso=progresso)
    except Exception as erro:                                # qualquer falha vira "erro" no job, nunca um job preso em "rodando"
        resultado = {"estado": "erro", "mensagem": f"{type(erro).__name__}: {erro}",
                     "edicoes_novas": [], "julgados_novos": 0, "a_revisar": {}}

    fim = relogio()
    espera = ESPERA_COM_NOVIDADE if resultado["estado"] == "ok" else ESPERA_SEM_NOVIDADE
    job.update(estado=resultado["estado"], etapa="", mensagem=resultado["mensagem"], finalizado_em=iso(fim),
               proxima_verificacao_em=iso(fim + espera),
               resultado={k: resultado.get(k) for k in ("edicoes_novas", "julgados_novos", "a_revisar")})
    tentar(container.upsert_item, job)
    return job
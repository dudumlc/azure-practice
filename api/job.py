"""
O "job" é o registro da última verificação: um único documento (id "job", partição "_meta") no Cosmos.

A API do Static Web App (api/function_app.py) cria o job como "na_fila" e chama executar_job() na mesma requisição,
que o leva a "rodando" e depois ao estado final. A tela só lê esse documento.
A espera entre verificações vem pronta no próprio job (proxima_verificacao_em), calculada aqui ao terminar
(depois de uma edição nova: até a próxima terça-feira; ver DIA_DO_INFORMATIVO).

    estado:  na_fila | rodando | ok | sem_novidade | parcial | bloqueio | erro
    parcial = acabou o tempo da chamada no meio da lista de edições; o que foi gravado fica e o botão libera na hora.
"""
from datetime import datetime, timedelta, timezone

from atualizar import atualizar, tentar

JOB_ID = "job"
JOB_PARTICAO = "_meta"
BRT = timezone(timedelta(hours=-3))          # horário de Brasília (sem horário de verão)
DIA_DO_INFORMATIVO = 1                       # dia em que o STJ publica: segunda = 0, terça = 1, ...
ESPERA_SEM_NOVIDADE = timedelta(hours=1)     # nada novo, bloqueio ou erro: pode tentar de novo logo
ESPERA_PARCIAL = timedelta(0)                # parou por tempo: o botão libera na hora para continuar
TRAVA_RODANDO = timedelta(minutes=2)         # "rodando" há mais que isso = travado (a API corta a chamada em ~45 s)
INTERVALO_PROGRESSO = 3.0                    # segundos entre gravações da etapa, para não gastar RU à toa


def agora():
    return datetime.now(timezone.utc)


def proxima_publicacao(momento):
    """Meia-noite (Brasília) do próximo dia de publicação do STJ depois de `momento`.
    Depois de uma edição nova, o botão fica travado até lá."""
    local = momento.astimezone(BRT)
    dias = (DIA_DO_INFORMATIVO - local.weekday()) % 7 or 7          # se hoje já é o dia, vale o da semana que vem
    return (local + timedelta(days=dias)).replace(hour=0, minute=0, second=0, microsecond=0)


def iso(momento):
    return momento.isoformat(timespec="seconds")


def ler_job(container):
    try:
        return tentar(container.read_item, JOB_ID, partition_key=JOB_PARTICAO)
    except Exception as erro:
        if "NotFound" in type(erro).__name__:
            return None
        raise


def executar_job(container, rodar=atualizar, relogio=agora, orcamento_s=None):
    """Roda a atualização e registra o andamento no job. Devolve o job final.

    orcamento_s: segundos que a atualização pode usar (repassado a atualizar(); None = sem limite)."""
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
        extra = {} if orcamento_s is None else {"orcamento_s": orcamento_s}
        resultado = rodar(container, progresso=progresso, **extra)
    except Exception as erro:                                # qualquer falha vira "erro" no job, nunca um job preso em "rodando"
        resultado = {"estado": "erro", "mensagem": f"{type(erro).__name__}: {erro}",
                     "edicoes_novas": [], "julgados_novos": 0, "a_revisar": {}}

    fim = relogio()
    libera_em = {"ok": proxima_publicacao(fim), "parcial": fim + ESPERA_PARCIAL}.get(resultado["estado"], fim + ESPERA_SEM_NOVIDADE)
    job.update(estado=resultado["estado"], etapa="", mensagem=resultado["mensagem"], finalizado_em=iso(fim),
               proxima_verificacao_em=iso(libera_em),
               resultado={k: resultado.get(k) for k in ("edicoes_novas", "julgados_novos", "a_revisar")})
    tentar(container.upsert_item, job)
    return job
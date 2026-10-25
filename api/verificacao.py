"""
Regras do botão "Verificar agora" (sem dependência de Azure Functions, para poder ser testado sozinho).

  solicitar_verificacao(): decide se a verificação pode começar; se puder, cria o job "na_fila" e enfileira.
  estado_da_verificacao(): o que a tela mostra (andamento, resultado, se o botão está liberado).

O documento "job" (id "job", partição "_meta") é o mesmo que a Function de ingestão atualiza.
A espera entre verificações vem de job["proxima_verificacao_em"], calculada pela Function ao terminar.
"""
from datetime import datetime, timedelta, timezone

from azure.core import MatchConditions
from azure.cosmos import exceptions

JOB_ID = "job"
JOB_PARTICAO = "_meta"
TRAVA_FILA = timedelta(minutes=15)       # "na fila" ou "rodando" há mais que isso = travado, deixa tentar de novo
CAMPOS_PUBLICOS = ("estado", "criado_em", "iniciado_em", "finalizado_em", "etapa", "mensagem", "resultado",
                   "proxima_verificacao_em")


def agora():
    return datetime.now(timezone.utc)


def iso(momento):
    return momento.isoformat(timespec="seconds")


def _data(texto):
    return datetime.fromisoformat(texto) if texto else None


def ler_job(container):
    try:
        return container.read_item(JOB_ID, partition_key=JOB_PARTICAO)
    except exceptions.CosmosResourceNotFoundError:
        return None


def _em_andamento(job, momento):
    """True se há uma verificação na fila ou rodando que ainda não deve ser considerada travada."""
    if job.get("estado") not in ("na_fila", "rodando"):
        return False
    inicio = _data(job.get("iniciado_em") or job.get("criado_em"))
    return inicio is not None and momento - inicio < TRAVA_FILA


def _publico(job, momento):
    """O que a tela recebe: os campos do job + se o botão está liberado e, se não, por quê."""
    saida = {c: (job or {}).get(c) for c in CAMPOS_PUBLICOS}
    saida["agora"] = iso(momento)
    if not job:
        saida.update(estado="nunca", pode_verificar=True, motivo=None)
        return saida
    if _em_andamento(job, momento):
        saida.update(pode_verificar=False, motivo="em_andamento")
    elif _data(job.get("proxima_verificacao_em")) and momento < _data(job["proxima_verificacao_em"]):
        saida.update(pode_verificar=False, motivo="espera")
    else:
        saida.update(pode_verificar=True, motivo=None)
    return saida


def estado_da_verificacao(container, momento=None):
    momento = momento or agora()
    return _publico(ler_job(container), momento)


def solicitar_verificacao(container, enfileirar, momento=None):
    """-> (status HTTP, corpo). 202 = enfileirada; 409 = já em andamento; 429 = dentro do período de espera."""
    momento = momento or agora()
    job = ler_job(container)
    atual = _publico(job, momento)
    if not atual["pode_verificar"]:
        return (409 if atual["motivo"] == "em_andamento" else 429), atual

    novo = {"id": JOB_ID, "edicao": JOB_PARTICAO, "estado": "na_fila", "criado_em": iso(momento),
            "iniciado_em": None, "finalizado_em": None, "etapa": "Na fila…", "mensagem": "", "resultado": None,
            "proxima_verificacao_em": None}
    try:
        if job is None:
            container.create_item(novo)                       # falha se outro pedido criou ao mesmo tempo
        else:                                                  # só troca se ninguém alterou o job nesse meio-tempo
            container.replace_item(job["id"], novo, etag=job["_etag"], match_condition=MatchConditions.IfNotModified)
    except exceptions.CosmosHttpResponseError as erro:
        if erro.status_code in (409, 412):
            return 409, {**_publico(ler_job(container), momento), "motivo": "em_andamento"}
        raise

    try:
        enfileirar()
    except Exception:
        # não conseguiu enfileirar: não deixa o job preso e libera o botão de novo
        container.upsert_item({**novo, "estado": "erro", "etapa": "", "finalizado_em": iso(momento),
                               "mensagem": "Não foi possível iniciar a verificação. Tente de novo.",
                               "proxima_verificacao_em": iso(momento)})
        raise
    return 202, _publico(novo, momento)
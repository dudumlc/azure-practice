"""
Atualização da base: acha as edições novas do Informativo no STJ, extrai e grava no Cosmos DB.

É o "miolo" da atualização (job.executar_job() chama atualizar(), e a API em api/function_app.py chama o job).
Fica separado de propósito: não conhece HTTP nem Azure Functions, então roda e é testado sozinho.

Por edição, nesta ordem: lê o PDF -> confere a contagem -> grava os julgados -> atualiza o `meta`.
O `meta` é atualizado a cada edição, então se o processo cair no meio, a próxima execução continua de onde parou.
Uma edição com algo estranho é gravada mesmo assim e marcada em `edicoes_a_revisar` (com o motivo em `revisar_motivos`).

Limite de tempo: a API do Static Web App corta a chamada em ~45 s. Com `orcamento_s`, a atualização não começa
uma edição que provavelmente passaria do orçamento: para com estado "parcial" e basta chamar de novo.

Uso local (precisa de COSMOS_CONNECTION_STRING):
    python api/atualizar.py
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from baixar_informativos import BloqueioSTJ, TempoEsgotado, USER_AGENT, novas_edicoes  # noqa: E402
from extrator_informativos import ler_pdf_detalhado  # noqa: E402
from registro_julgado import META_ID, META_PARTICAO, montar_docs, sem_repetidos  # noqa: E402

VERSAO_PARSER = "pdf-2026-10"
MARGEM_ESTIMATIVA = 1.3      # a próxima edição pode demorar 30% mais que a mais demorada até agora


def agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tentar(funcao, *args, tentativas=5, **kwargs):
    """Repete a chamada ao Cosmos quando a conexão cai, esperando mais a cada vez."""
    for n in range(1, tentativas + 1):
        try:
            return funcao(*args, **kwargs)
        except Exception as erro:
            if n == tentativas or "NotFound" in type(erro).__name__:
                raise
            time.sleep(2 ** n)


def ler_meta(container):
    try:
        return tentar(container.read_item, META_ID, partition_key=META_PARTICAO)
    except Exception as erro:
        if "NotFound" in type(erro).__name__:
            return None
        raise


def problemas_da_edicao(blocos, n_processo_pdf):
    """Motivos para uma pessoa olhar a edição. Lista vazia = tudo certo."""
    problemas = []
    if not blocos:
        return ["nenhum julgado extraído"]
    criados = sum("PROCESSO ausente no PDF" in b["OBS"] for b in blocos)   # a edição não traz a linha PROCESSO
    if len(blocos) - criados != n_processo_pdf:
        problemas.append(f"contagem: {n_processo_pdf} linhas PROCESSO no PDF x {len(blocos) - criados} blocos")
    if any("RAMO DO DIREITO" in b["TEMA"] for b in blocos):
        problemas.append("TEMA com 'RAMO DO DIREITO' dentro (blocos colados)")
    if any("fora de ordem" in b["OBS"] for b in blocos):
        problemas.append("marcadores fora de ordem")
    if any("sem SEÇÃO" in b["OBS"] for b in blocos):
        problemas.append("julgado sem seção (título de seção novo?)")
    return problemas


def gravar_edicao(container, edicao, docs):
    """Troca a edição inteira: grava os documentos novos e apaga os que sumiram (rodar de novo não duplica).

    Devolve True se a edição já existia na base (reprocessamento), para o meta não contá-la duas vezes."""
    novos = {d["id"] for d in docs}
    existentes = tentar(lambda: list(container.query_items(
        "SELECT c.id FROM c WHERE c.edicao = @e", parameters=[{"name": "@e", "value": edicao}], partition_key=edicao)))
    for item in existentes:
        if item["id"] not in novos:
            tentar(container.delete_item, item["id"], partition_key=edicao)
    for d in docs:
        tentar(container.upsert_item, d)
    return bool(existentes)


def total_na_base(container):
    return list(tentar(lambda: list(container.query_items(
        "SELECT VALUE COUNT(1) FROM c WHERE c.edicao != '_meta'", enable_cross_partition_query=True))))[0]


def registrar_edicao(container, meta, edicao, problemas, ja_existia=False):
    """Atualiza o meta depois de gravar UMA edição."""
    meta["ultima_edicao"] = max(edicao, meta.get("ultima_edicao") or "") if edicao.isdigit() else meta.get("ultima_edicao")
    if not ja_existia:
        meta["total_edicoes"] = (meta.get("total_edicoes") or 0) + 1
    meta["total_julgados"] = total_na_base(container)
    meta["atualizado_em"] = meta["ultima_verificacao"] = agora()
    meta["versao_parser"] = VERSAO_PARSER
    if problemas:
        meta["edicoes_a_revisar"] = sorted(set(meta.get("edicoes_a_revisar") or []) | {edicao})
        meta["revisar_motivos"] = {**(meta.get("revisar_motivos") or {}), edicao: problemas}
    tentar(container.upsert_item, meta)


def atualizar(container, sessao=None, novas=novas_edicoes, ler=ler_pdf_detalhado, progresso=None,
              orcamento_s=None, relogio=time.monotonic):
    """Procura edições novas e grava. Devolve um dict para o job mostrar na tela:

        estado: "ok" (tinha edição nova) | "sem_novidade" | "parcial" (acabou o tempo; chame de novo para continuar)
                | "bloqueio" (STJ recusou; o que veio fica gravado) | "erro"
        edicoes_novas, julgados_novos, a_revisar (edição -> motivos), mensagem

    orcamento_s: segundos que esta chamada pode usar. Antes de começar cada edição depois da primeira, confere se
    ainda dá tempo (estimativa: a edição mais demorada até ali, com margem); se não der, para com "parcial".
    """
    progresso = progresso or (lambda texto: None)
    resultado = {"estado": "sem_novidade", "edicoes_novas": [], "julgados_novos": 0, "a_revisar": {}, "mensagem": ""}
    inicio = relogio()

    meta = ler_meta(container)
    if not meta or not meta.get("ultima_edicao"):
        resultado.update(estado="erro", mensagem="A base ainda não tem o documento 'meta': faça a carga inicial.")
        return resultado
    ultima = int(meta["ultima_edicao"])

    if sessao is None:
        import requests
        sessao = requests.Session()
        sessao.headers["User-Agent"] = USER_AGENT

    progresso(f"Procurando edições depois da {ultima:04d}…")
    edicao = None
    parou_por_tempo = False
    extra = {} if orcamento_s is None else {"prazo": inicio + orcamento_s}   # limite também para esperas de rede
    try:
        iterador = iter(novas(sessao, ultima, **extra))
        maior_custo = 0.0                                                       # segundos da edição mais demorada
        while True:
            if (orcamento_s is not None and resultado["edicoes_novas"]
                    and relogio() - inicio + maior_custo * MARGEM_ESTIMATIVA > orcamento_s):
                parou_por_tempo = True
                break
            comeco = relogio()
            try:
                edicao, pdf = next(iterador)
            except StopIteration:
                break
            progresso(f"Edição {edicao}: lendo o PDF…")
            blocos, n_processo_pdf = ler(pdf)
            problemas = problemas_da_edicao(blocos, n_processo_pdf)
            docs = montar_docs(edicao, sem_repetidos(blocos))
            progresso(f"Edição {edicao}: gravando {len(docs)} julgados…")
            ja_existia = gravar_edicao(container, edicao, docs)
            registrar_edicao(container, meta, edicao, problemas, ja_existia)
            resultado["edicoes_novas"].append(edicao)
            resultado["julgados_novos"] += len(docs)
            if problemas:
                resultado["a_revisar"][edicao] = problemas
            maior_custo = max(maior_custo, relogio() - comeco)
    except BloqueioSTJ as erro:
        resultado.update(estado="bloqueio", mensagem=f"O site do STJ recusou o pedido ({erro}). Tente de novo mais tarde.")
    except TempoEsgotado:
        parou_por_tempo = True
    except Exception as erro:   # falha numa edição: para aqui; o que já foi gravado fica e a próxima execução continua
        resultado.update(estado="erro", mensagem=f"Falha na edição {edicao}: {type(erro).__name__}: {erro}")

    if resultado["estado"] not in ("bloqueio", "erro"):
        if parou_por_tempo:
            resultado["estado"] = "parcial"
        else:
            resultado["estado"] = "ok" if resultado["edicoes_novas"] else "sem_novidade"

    if not resultado["edicoes_novas"] or resultado["estado"] != "ok":
        meta["ultima_verificacao"] = agora()      # registra que a verificação aconteceu, mesmo sem novidade
        tentar(container.upsert_item, meta)
    novas_txt = ", ".join(resultado["edicoes_novas"])
    if resultado["estado"] == "ok":
        n = len(resultado["edicoes_novas"])
        resultado["mensagem"] = f"{n} edição(ões) nova(s): {novas_txt}; {resultado['julgados_novos']} julgados."
    elif resultado["estado"] == "parcial":
        if resultado["edicoes_novas"]:
            resultado["mensagem"] = (f"Gravei {novas_txt} ({resultado['julgados_novos']} julgados), mas o tempo desta "
                                     f"chamada acabou. Aperte o botão de novo para continuar.")
        else:
            resultado["mensagem"] = ("O site do STJ demorou a responder e o tempo desta chamada acabou. "
                                     "Aperte o botão de novo.")
    elif resultado["estado"] == "sem_novidade":
        resultado["mensagem"] = "Nenhuma edição nova no site do STJ."
    return resultado


if __name__ == "__main__":
    from azure.cosmos import CosmosClient
    cs = os.environ.get("COSMOS_CONNECTION_STRING")
    if not cs:
        sys.exit("Defina COSMOS_CONNECTION_STRING.")
    c = CosmosClient.from_connection_string(cs).get_database_client("jurisprudencia").get_container_client("julgados")
    print(json.dumps(atualizar(c, progresso=print), ensure_ascii=False, indent=2))
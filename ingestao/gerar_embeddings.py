"""
Gera os vetores dos julgados do Cosmos que ainda não têm (campo `embedding`).

- Só pega documentos sem vetor, então pode rodar quantas vezes quiser: sempre continua do que falta.
- Grava a cada lote de 50: se parar no meio (cota do dia, Ctrl+C, queda de rede), nada do que já foi feito se perde.
- Só acrescenta os campos embedding, embedding_modelo e embedding_dim; o resto do documento não é tocado.
- Mais novos primeiro (edição decrescente).

Uso (raiz do projeto, com COSMOS_CONNECTION_STRING e GEMINI_API_KEY definidas com `set`, no mesmo terminal):
    python ingestao/gerar_embeddings.py --dry-run             # só conta quantos faltam
    python ingestao/gerar_embeddings.py --limite 900          # plano gratuito: ~1.000 por dia; rode uma vez por dia
    python ingestao/gerar_embeddings.py --pausa 1             # com cobrança ativada no Google AI Studio: termina em minutos
"""
import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
from embeddings import (CotaEsgotada, DIMENSOES, ErroGemini, LOTE, MODELO, PAUSA_ENTRE_LOTES,  # noqa: E402
                        embutir, texto_do_julgado)


def tentar(funcao, *args, tentativas=5, **kwargs):
    """Repete a chamada quando a conexão cai ou o Cosmos pede para esperar (429), esperando mais a cada vez."""
    for n in range(1, tentativas + 1):
        try:
            return funcao(*args, **kwargs)
        except Exception as erro:
            if n == tentativas or "NotFound" in type(erro).__name__:
                raise
            time.sleep(2 ** n)


def pendentes(container):
    docs = tentar(lambda: list(container.query_items(
        'SELECT c.id, c.edicao, c.tema, c.destaque FROM c WHERE c.edicao != "_meta" AND NOT IS_DEFINED(c.embedding)',
        enable_cross_partition_query=True)))
    return sorted(docs, key=lambda d: d["edicao"], reverse=True)


def gerar(container, chave_api, limite=None, lote=LOTE, pausa=PAUSA_ENTRE_LOTES, dry_run=False,
          sessao=requests, esperar=time.sleep, progresso=print):
    docs = pendentes(container)
    com_texto = [d for d in docs if texto_do_julgado(d)]
    if limite:
        com_texto = com_texto[:limite]
    resumo = {"pendentes": len(docs), "sem_texto": len(docs) - len([d for d in docs if texto_do_julgado(d)]),
              "nesta_rodada": len(com_texto), "gravados": 0, "parou_por": None}
    if dry_run or not com_texto:
        return resumo
    feitos = [0]

    def gravar(_textos, vetores):
        for v in vetores:
            d = com_texto[feitos[0]]
            tentar(container.patch_item, item=d["id"], partition_key=d["edicao"], patch_operations=[
                {"op": "add", "path": "/embedding", "value": [round(x, 6) for x in v]},
                {"op": "add", "path": "/embedding_modelo", "value": MODELO},
                {"op": "add", "path": "/embedding_dim", "value": DIMENSOES}])
            feitos[0] += 1
        resumo["gravados"] = feitos[0]

    try:
        embutir([texto_do_julgado(d) for d in com_texto], "RETRIEVAL_DOCUMENT", chave_api, sessao=sessao,
                esperar=esperar, progresso=progresso, ao_terminar_lote=gravar, lote=lote, pausa=pausa)
    except CotaEsgotada as erro:
        resumo["parou_por"] = str(erro)
    return resumo


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="só conta, não chama o Gemini nem grava")
    ap.add_argument("--limite", type=int, default=None, help="no máximo N documentos nesta rodada")
    ap.add_argument("--pausa", type=float, default=PAUSA_ENTRE_LOTES, help="segundos entre lotes (padrão: ritmo do plano gratuito)")
    args = ap.parse_args()
    cs, chave_api = os.environ.get("COSMOS_CONNECTION_STRING"), os.environ.get("GEMINI_API_KEY")
    if not cs or (not chave_api and not args.dry_run):
        sys.exit("Defina COSMOS_CONNECTION_STRING e GEMINI_API_KEY com `set`, no mesmo terminal.")
    from azure.cosmos import CosmosClient
    c = (CosmosClient.from_connection_string(cs).get_database_client("jurisprudencia")
         .get_container_client(os.environ.get("COSMOS_CONTAINER", "acervo")))
    try:
        r = gerar(c, chave_api, limite=args.limite, pausa=args.pausa, dry_run=args.dry_run)
    except ErroGemini as erro:
        sys.exit(f"\n{erro}\nO que já foi gravado ficou salvo; é só rodar de novo depois de corrigir.")
    print(f"Sem vetor: {r['pendentes']} (sem texto para vetorizar: {r['sem_texto']}) | nesta rodada: {r['nesta_rodada']} | gravados: {r['gravados']}")
    if r["parou_por"]:
        print(f"Parou: {r['parou_por']}. O que já foi gravado ficou salvo; rode de novo depois (a cota diária zera de madrugada).")
    elif not args.dry_run:
        faltam = r["pendentes"] - r["sem_texto"] - r["gravados"]
        print("Pronto." if faltam <= 0 else f"Faltam {faltam}: rode de novo para continuar.")

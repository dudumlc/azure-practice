"""
Cria o container `acervo` (com busca por vetores) e copia para ele o que está no `julgados`.

- O `julgados` NÃO é alterado nem apagado.
- Copia só o que ainda não está no `acervo`: pode rodar de novo (se cair no meio, continua), e nunca
  sobrescreve um documento que já está lá (por exemplo, um que já ganhou vetor).
- O documento `job` (estado temporário da verificação) não é copiado; o `meta` é.
- Os vetores já gerados no teste (outputs/cache_embeddings.json) são reaproveitados.
- No fim confere as contagens e faz uma consulta vetorial de teste.

Uso (raiz do projeto, com COSMOS_CONNECTION_STRING definida):
    python ingestao/migrar_para_acervo.py --dry-run     # só conta, não cria nem grava nada
    python ingestao/migrar_para_acervo.py               # cria o acervo, copia e confere
"""
import argparse
import hashlib
import json
import os
import sys
import time

BANCO = "jurisprudencia"
ORIGEM = "julgados"
DESTINO = "acervo"
CHAVE_PARTICAO = "/edicao"
THROUGHPUT = 400                 # RU/s; somado aos 400 do julgados fica em 800, dentro dos 1000 gratuitos

# Precisam ser os mesmos do teste_embeddings.py, senão o cache não é reaproveitado
MODELO = "gemini-embedding-001"
DIMENSOES = 768
MAX_CHARS = 6000
CACHE = os.path.join("outputs", "cache_embeddings.json")


def politica_vetores():
    return {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float32",
                                  "distanceFunction": "cosine", "dimensions": DIMENSOES}]}


def politica_indexacao():
    return {
        "indexingMode": "consistent", "automatic": True,
        "includedPaths": [{"path": "/*"}],
        # o vetor tem índice próprio; o inteiro teor é grande e a busca usa só tema_busca/destaque_busca
        "excludedPaths": [{"path": '/"_etag"/?'}, {"path": "/embedding/*"}, {"path": "/inteiro_teor/*"}],
        "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}],
    }


def tentar(funcao, *args, tentativas=5, **kwargs):
    """Repete a chamada quando a conexão cai ou o Cosmos pede para esperar (429), esperando mais a cada vez."""
    for n in range(1, tentativas + 1):
        try:
            return funcao(*args, **kwargs)
        except Exception as erro:
            if n == tentativas or "NotFound" in type(erro).__name__:
                raise
            time.sleep(2 ** n)


def texto_do_julgado(doc):
    return f"{doc.get('tema', '')}\n{doc.get('destaque', '')}".strip()[:MAX_CHARS]


def chave_cache(texto):
    return hashlib.sha1(f"{MODELO}|{DIMENSOES}|RETRIEVAL_DOCUMENT|{texto}".encode()).hexdigest()


def preparar(doc, cache):
    """Tira os campos internos do Cosmos (_etag, _ts...) e, se o vetor já existe no cache, anexa."""
    novo = {k: v for k, v in doc.items() if not k.startswith("_")}
    vetor = cache.get(chave_cache(texto_do_julgado(doc))) if doc.get("edicao") != "_meta" else None
    if vetor and len(vetor) == DIMENSOES:
        novo["embedding"] = vetor
        novo["embedding_modelo"] = MODELO
        novo["embedding_dim"] = DIMENSOES
    return novo


def ja_no_destino(destino):
    try:
        itens = tentar(lambda: list(destino.query_items("SELECT c.id, c.edicao FROM c",
                                                        enable_cross_partition_query=True)))
    except Exception as erro:
        if "NotFound" in type(erro).__name__:
            return set()
        raise
    return {(i["edicao"], i["id"]) for i in itens}


def copiar(origem, destino, cache, dry_run=False, progresso=print):
    existentes = ja_no_destino(destino) if destino is not None else set()
    docs = tentar(lambda: list(origem.query_items("SELECT * FROM c", enable_cross_partition_query=True)))
    resumo = {"origem": len(docs), "ja_no_destino": 0, "copiados": 0, "com_vetor": 0, "ignorados": 0}
    for doc in docs:
        if doc["id"] == "job":
            resumo["ignorados"] += 1
            continue
        if (doc["edicao"], doc["id"]) in existentes:
            resumo["ja_no_destino"] += 1
            continue
        novo = preparar(doc, cache)
        resumo["copiados"] += 1
        resumo["com_vetor"] += "embedding" in novo
        if not dry_run:
            tentar(destino.upsert_item, novo)
            if resumo["copiados"] % 200 == 0:
                progresso(f"  {resumo['copiados']} copiados…")
    return resumo


def contagens(container):
    linhas = tentar(lambda: list(container.query_items(
        'SELECT c.tipo, COUNT(1) AS n FROM c WHERE c.id != "job" GROUP BY c.tipo',
        enable_cross_partition_query=True)))
    return {l.get("tipo", "(sem tipo)"): l["n"] for l in linhas}


def testar_busca_vetorial(destino):
    """Pega um documento com vetor e pergunta ao Cosmos quem é o mais parecido: tem que ser ele mesmo."""
    amostra = tentar(lambda: list(destino.query_items(
        "SELECT TOP 1 c.id, c.embedding FROM c WHERE IS_DEFINED(c.embedding)", enable_cross_partition_query=True)))
    if not amostra:
        return None
    achados = tentar(lambda: list(destino.query_items(
        "SELECT TOP 1 c.id, VectorDistance(c.embedding, @v) AS similaridade FROM c "
        "WHERE IS_DEFINED(c.embedding) ORDER BY VectorDistance(c.embedding, @v)",
        parameters=[{"name": "@v", "value": amostra[0]["embedding"]}], enable_cross_partition_query=True)))
    return achados[0]["id"] == amostra[0]["id"], achados[0]["similaridade"]


def carregar_cache(caminho):
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    return {}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="só conta, não cria nem grava")
    ap.add_argument("--origem", default=ORIGEM)
    ap.add_argument("--destino", default=DESTINO)
    ap.add_argument("--cache", default=CACHE)
    args = ap.parse_args()
    cs = os.environ.get("COSMOS_CONNECTION_STRING")
    if not cs:
        sys.exit("Defina COSMOS_CONNECTION_STRING.")
    from azure.cosmos import CosmosClient, PartitionKey
    banco = CosmosClient.from_connection_string(cs).get_database_client(BANCO)
    origem = banco.get_container_client(args.origem)
    cache = carregar_cache(args.cache)
    print(f"{len(cache)} vetor(es) no cache.")

    if args.dry_run:
        try:
            destino = banco.get_container_client(args.destino)
            destino.read()
        except Exception:
            destino = None
            print(f"O container {args.destino!r} ainda não existe (seria criado).")
    else:
        destino = banco.create_container_if_not_exists(
            id=args.destino, partition_key=PartitionKey(path=CHAVE_PARTICAO),
            indexing_policy=politica_indexacao(), vector_embedding_policy=politica_vetores(),
            offer_throughput=THROUGHPUT)
        print(f"Container {args.destino!r} pronto.")

    r = copiar(origem, destino, cache, dry_run=args.dry_run)
    verbo = "seriam copiados" if args.dry_run else "copiados"
    print(f"Origem: {r['origem']} | já no destino: {r['ja_no_destino']} | {verbo}: {r['copiados']} "
          f"(com vetor do cache: {r['com_vetor']}) | ignorados (job): {r['ignorados']}")
    if args.dry_run:
        sys.exit(0)

    a, b = contagens(origem), contagens(destino)
    print(f"Contagem por tipo — origem: {a} | destino: {b}")
    if a != b:
        sys.exit("ATENÇÃO: as contagens são diferentes. Rode o script de novo (ele copia só o que falta).")
    print("Contagens iguais.")
    teste = testar_busca_vetorial(destino)
    if teste is None:
        print("Busca vetorial: sem vetores no destino para testar (normal se o cache estava vazio).")
    else:
        print(f"Busca vetorial: {'OK' if teste[0] else 'FALHOU'} (similaridade consigo mesmo: {teste[1]:.4f}).")

"""
Acrescenta o campo `tipo` aos documentos que já estão no Cosmos (rodar UMA vez; pode repetir sem problema).

Só mexe nos julgados que ainda não têm `tipo` (o `meta` e o `job` ficam de fora) e só acrescenta o campo:
o resto do documento não é tocado. Se cair no meio, é só rodar de novo: ele continua dos que faltam.

Uso (raiz do projeto, com COSMOS_CONNECTION_STRING definida):
    python ingestao/adicionar_tipo.py --dry-run     # só conta quantos faltam
    python ingestao/adicionar_tipo.py               # grava
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
from registro_julgado import TIPO_INFORMATIVO  # noqa: E402


def tentar(funcao, *args, tentativas=5, **kwargs):
    """Repete a chamada quando a conexão cai ou o Cosmos pede para esperar (429), esperando mais a cada vez."""
    for n in range(1, tentativas + 1):
        try:
            return funcao(*args, **kwargs)
        except Exception as erro:
            if n == tentativas or "NotFound" in type(erro).__name__:
                raise
            time.sleep(2 ** n)


def adicionar_tipo(container, tipo=TIPO_INFORMATIVO, dry_run=False, progresso=print):
    """Devolve quantos documentos receberam (ou receberiam, no dry-run) o campo."""
    faltam = tentar(lambda: list(container.query_items(
        'SELECT c.id, c.edicao FROM c WHERE c.edicao != "_meta" AND NOT IS_DEFINED(c.tipo)',
        enable_cross_partition_query=True)))
    progresso(f"{len(faltam)} documento(s) sem o campo `tipo`.")
    if dry_run:
        return len(faltam)
    for i, doc in enumerate(faltam, 1):
        tentar(container.patch_item, item=doc["id"], partition_key=doc["edicao"],
               patch_operations=[{"op": "add", "path": "/tipo", "value": tipo}])
        if i % 200 == 0:
            progresso(f"  {i}/{len(faltam)}")
    progresso(f"Pronto: {len(faltam)} documento(s) atualizado(s) com tipo={tipo!r}.")
    return len(faltam)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="só conta, não grava")
    args = ap.parse_args()
    cs = os.environ.get("COSMOS_CONNECTION_STRING")
    if not cs:
        sys.exit("Defina COSMOS_CONNECTION_STRING.")
    from azure.cosmos import CosmosClient
    c = CosmosClient.from_connection_string(cs).get_database_client("jurisprudencia").get_container_client(os.environ.get("COSMOS_CONTAINER", "acervo"))
    adicionar_tipo(c, dry_run=args.dry_run)
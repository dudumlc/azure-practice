"""
Carga local: Excel do extrator -> Cosmos DB. Não faz parte da Function (usa pandas).

Uso (a partir da raiz do projeto):
    python scripts/carga_excel_cosmos.py output/jurisprudencia_extraida.xlsx --dry-run   # só confere, não grava
    python scripts/carga_excel_cosmos.py output/jurisprudencia_extraida.xlsx             # grava

A connection string vem de COSMOS_CONNECTION_STRING (variável de ambiente) ou de api/local.settings.json.
Pode rodar de novo: cada edição é substituída inteira (documentos novos entram, os que sumiram saem).
Para recarregar só algumas edições:  --edicoes 0683,0686,0693
"""
import argparse
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingestao"))
from registro_julgado import edicao_do_arquivo, eh_aviso, montar_docs, montar_meta  # noqa: E402

VERSAO_PARSER = "excel-2026-10-secao"
CHAVE = ["ARQUIVO", "PROCESSO", "SECAO", "TEMA", "DESTAQUE", "INTEIRO_TEOR", "OBS"]


def tentar(funcao, *args, tentativas=6, **kwargs):
    """Repete a chamada ao Cosmos quando a conexão cai ('Remote end closed connection'), esperando mais a cada vez."""
    for n in range(1, tentativas + 1):
        try:
            return funcao(*args, **kwargs)
        except Exception as erro:
            if n == tentativas or "NotFound" in type(erro).__name__:
                raise
            espera = 2 ** n
            print(f"  conexão falhou ({type(erro).__name__}); nova tentativa {n}/{tentativas - 1} em {espera}s")
            time.sleep(espera)


def ler_excel(caminho):
    """Excel (uma linha por ramo) -> {edicao: [blocos]}, na ordem do informativo."""
    df = pd.read_excel(caminho).fillna("")
    if "SECAO" not in df.columns:      # Excel gerado antes da classificação por seção
        df["SECAO"] = ""
    df = df.astype({c: str for c in CHAVE + ["RAMO_DO_DIREITO"]})
    por_edicao = {}
    for chave, g in df.groupby(CHAVE, sort=False):
        bloco = dict(zip(CHAVE, chave))
        bloco["RAMO_DO_DIREITO"] = ",".join(g["RAMO_DO_DIREITO"])
        por_edicao.setdefault(edicao_do_arquivo(bloco["ARQUIVO"]), []).append(bloco)
    return por_edicao


def connection_string(settings):
    cs = os.environ.get("COSMOS_CONNECTION_STRING")
    if cs:
        return cs
    if os.path.exists(settings):
        with open(settings, encoding="utf-8") as fh:
            return json.load(fh).get("Values", {}).get("COSMOS_CONNECTION_STRING")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("excel")
    ap.add_argument("--dry-run", action="store_true", help="monta os documentos e confere, sem gravar")
    ap.add_argument("--edicoes", default="", help="só estas edições, separadas por vírgula (ex.: 0683,0686)")
    ap.add_argument("--settings", default="api/local.settings.json")
    ap.add_argument("--database", default="jurisprudencia")
    ap.add_argument("--container", default="julgados")
    args = ap.parse_args()

    por_edicao = ler_excel(args.excel)
    escolhidas = {e.strip() for e in args.edicoes.split(",") if e.strip()}
    if escolhidas:
        faltam = escolhidas - set(por_edicao)
        if faltam:
            print(f"Edições que não estão no Excel: {sorted(faltam)}")
            return 2
        por_edicao = {ed: b for ed, b in por_edicao.items() if ed in escolhidas}
    docs_por_edicao = {ed: montar_docs(ed, blocos) for ed, blocos in sorted(por_edicao.items())}
    total = sum(len(d) for d in docs_por_edicao.values())
    avisos = [(ed, b["PROCESSO"][:70]) for ed, blocos in sorted(por_edicao.items()) for b in blocos if eh_aviso(b)]
    if avisos:
        print(f"{len(avisos)} avisos do informativo (nota excluída/alterada, errata) não entram na base:")
        for ed, proc in avisos:
            print(f"   {ed}  {proc}")
    ids = [d["id"] for docs in docs_por_edicao.values() for d in docs]
    print(f"{len(docs_por_edicao)} edições, {total} julgados, {len(set(ids))} ids únicos.")
    if len(set(ids)) != total:
        print("ERRO: ids repetidos, nada foi gravado.")
        return 1
    if args.dry_run:
        print("Dry-run: nada gravado.")
        return 0

    cs = connection_string(args.settings)
    if not cs:
        print(f"Sem connection string: defina COSMOS_CONNECTION_STRING ou preencha {args.settings}.")
        return 2
    from azure.cosmos import CosmosClient

    container = CosmosClient.from_connection_string(cs).get_database_client(args.database).get_container_client(args.container)

    t0 = time.time()
    for i, (ed, docs) in enumerate(docs_por_edicao.items(), 1):
        novos = {d["id"] for d in docs}
        existentes = tentar(lambda: list(container.query_items("SELECT c.id FROM c WHERE c.edicao = @e",
                                                               parameters=[{"name": "@e", "value": ed}], partition_key=ed)))
        for item in existentes:
            if item["id"] not in novos:
                tentar(container.delete_item, item["id"], partition_key=ed)
        for d in docs:
            tentar(container.upsert_item, d)
        if i % 20 == 0 or i == len(docs_por_edicao):
            print(f"  {i}/{len(docs_por_edicao)} edições ({time.time() - t0:.0f}s)")

    if escolhidas:
        no_banco = sum(list(container.query_items("SELECT VALUE COUNT(1) FROM c WHERE c.edicao = @e",
                                                  parameters=[{"name": "@e", "value": e}], partition_key=e))[0]
                       for e in docs_por_edicao)
        print(f"No Cosmos, nessas edições: {no_banco} julgados (esperado {total}).")
        print("O documento meta não foi alterado: rode a carga completa para atualizar os totais.")
        return 0 if no_banco == total else 1

    tentar(container.upsert_item, montar_meta(list(docs_por_edicao), total, VERSAO_PARSER))

    no_banco = list(container.query_items("SELECT VALUE COUNT(1) FROM c WHERE c.edicao != '_meta'",
                                          enable_cross_partition_query=True))[0]
    print(f"No Cosmos: {no_banco} julgados (esperado {total}).")
    return 0 if no_banco == total else 1


if __name__ == "__main__":
    sys.exit(main())
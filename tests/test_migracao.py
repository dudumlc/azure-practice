"""
Testa ingestao/migrar_para_acervo.py. Sem Azure e sem internet.

    python tests/test_migracao.py
"""
import os
import sys

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "ingestao"))
import migrar_para_acervo as m  # noqa: E402


class ContainerFalso:
    """Só o que o script usa: query_items (por trecho da consulta) e upsert_item."""
    def __init__(self, docs=()):
        self.docs = {(d["edicao"], d["id"]): dict(d) for d in docs}
        self.gravacoes = 0

    def query_items(self, query, parameters=None, enable_cross_partition_query=False):
        if query == "SELECT * FROM c":
            return [dict(d) for d in self.docs.values()]
        if query == "SELECT c.id, c.edicao FROM c":
            return [{"id": i, "edicao": e} for (e, i) in self.docs]
        if "GROUP BY c.tipo" in query:
            cont = {}
            for d in self.docs.values():
                if d["id"] != "job":
                    cont[d.get("tipo")] = cont.get(d.get("tipo"), 0) + 1
            return [({"tipo": t} if t else {}) | {"n": n} for t, n in cont.items()]
        raise AssertionError(query)

    def upsert_item(self, doc):
        self.docs[(doc["edicao"], doc["id"])] = dict(doc)
        self.gravacoes += 1


def vetor():
    return [0.1] * m.DIMENSOES


def origem():
    return ContainerFalso([
        {"id": "a1", "edicao": "0905", "tipo": "informativo-stj", "tema": "plano de saúde", "destaque": "x", "_etag": "e", "_ts": 1},
        {"id": "a2", "edicao": "0905", "tipo": "informativo-stj", "tema": "outro", "destaque": "y"},
        {"id": "meta", "edicao": "_meta", "ultima_edicao": "0905"},
        {"id": "job", "edicao": "_meta", "estado": "ok"},
    ])


def cache_com_a1():
    return {m.chave_cache(m.texto_do_julgado({"tema": "plano de saúde", "destaque": "x"})): vetor()}


def testa_politicas():
    assert m.politica_vetores()["vectorEmbeddings"][0]["dimensions"] == 768
    ind = m.politica_indexacao()
    assert ind["vectorIndexes"] == [{"path": "/embedding", "type": "quantizedFlat"}]
    assert {"path": "/embedding/*"} in ind["excludedPaths"]       # vetor fora do índice comum
    assert {"path": "/inteiro_teor/*"} in ind["excludedPaths"]


def testa_preparar():
    novo = m.preparar({"id": "a1", "edicao": "0905", "tema": "plano de saúde", "destaque": "x", "_etag": "e", "_ts": 1},
                      cache_com_a1())
    assert not any(k.startswith("_") for k in novo)
    assert novo["embedding_modelo"] == m.MODELO and novo["embedding_dim"] == 768 and len(novo["embedding"]) == 768
    sem = m.preparar({"id": "a2", "edicao": "0905", "tema": "outro", "destaque": "y"}, cache_com_a1())
    assert "embedding" not in sem and "embedding_modelo" not in sem
    meta = m.preparar({"id": "meta", "edicao": "_meta"}, cache_com_a1())
    assert "embedding" not in meta


def testa_copia_completa():
    o, d = origem(), ContainerFalso()
    r = m.copiar(o, d, cache_com_a1(), progresso=lambda *_: None)
    assert r == {"origem": 4, "ja_no_destino": 0, "copiados": 3, "com_vetor": 1, "ignorados": 1}, r
    assert ("_meta", "job") not in d.docs and ("_meta", "meta") in d.docs
    assert "embedding" in d.docs[("0905", "a1")]
    assert m.contagens(o) == m.contagens(d)


def testa_dry_run_nao_grava():
    d = ContainerFalso()
    r = m.copiar(origem(), d, cache_com_a1(), dry_run=True)
    assert r["copiados"] == 3 and d.gravacoes == 0
    r2 = m.copiar(origem(), None, {}, dry_run=True)               # destino ainda não existe
    assert r2["copiados"] == 3


def testa_repetir_nao_duplica_nem_sobrescreve():
    o, d = origem(), ContainerFalso()
    m.copiar(o, d, {}, progresso=lambda *_: None)
    d.docs[("0905", "a2")]["embedding"] = vetor()                  # vetor gerado depois da migração
    n = d.gravacoes
    r = m.copiar(o, d, {}, progresso=lambda *_: None)
    assert r["copiados"] == 0 and r["ja_no_destino"] == 3 and d.gravacoes == n
    assert "embedding" in d.docs[("0905", "a2")]                   # não foi apagado


def testa_continua_do_que_faltou():
    o, d = origem(), ContainerFalso([{"id": "a1", "edicao": "0905", "tipo": "informativo-stj"}])
    r = m.copiar(o, d, {}, progresso=lambda *_: None)
    assert r["copiados"] == 2 and r["ja_no_destino"] == 1


if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("testa_")]
    for t in testes:
        t()
        print("ok:", t.__name__)
    print(f"{len(testes)} testes passaram.")

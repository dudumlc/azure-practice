"""
Testa ingestao/atualizar.py sem Azure e sem internet: Cosmos de mentira (em memória) e PDFs de uma pasta.

Uso (raiz do projeto):
    python tests/test_atualizar.py data
Usa as edições 0800 e 0720 da pasta como se fossem "novas". Roda também com pytest, se estiver instalado.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingestao"))
from atualizar import atualizar  # noqa: E402
from baixar_informativos import BloqueioSTJ  # noqa: E402

PASTA = os.environ.get("PASTA_PDFS", "data")


class NaoEncontrado(Exception):
    pass


NaoEncontrado.__name__ = "CosmosResourceNotFoundError"


class ContainerFalso:
    """Só o que atualizar.py usa do container do Cosmos."""
    def __init__(self):
        self.docs = {}

    def read_item(self, id, partition_key):
        if (partition_key, id) not in self.docs:
            raise NaoEncontrado()
        return dict(self.docs[(partition_key, id)])

    def upsert_item(self, doc):
        self.docs[(doc["edicao"], doc["id"])] = dict(doc)

    def delete_item(self, id, partition_key):
        del self.docs[(partition_key, id)]

    def query_items(self, query, parameters=None, partition_key=None, enable_cross_partition_query=False):
        if query.startswith("SELECT c.id"):
            return [{"id": i} for (e, i) in self.docs if e == partition_key]
        if "COUNT(1)" in query:
            return [sum(1 for (e, _) in self.docs if e != "_meta")]
        raise AssertionError(query)


def pdf(edicao):
    with open(os.path.join(PASTA, f"Inf{edicao}.pdf"), "rb") as fh:
        return fh.read()


def base_com_meta(ultima="0719"):
    c = ContainerFalso()
    c.upsert_item({"id": "meta", "edicao": "_meta", "ultima_edicao": ultima, "total_edicoes": 10,
                   "total_julgados": 0, "ultima_verificacao": "2020-01-01T00:00:00+00:00"})
    return c


def novas_de(*edicoes, bloqueio_depois=False):
    def gerador(sessao, ultima):
        for e in edicoes:
            yield e, pdf(e)
        if bloqueio_depois:
            raise BloqueioSTJ("0999: HTTP 403")
    return gerador


def test_edicao_nova_e_gravada():
    c = base_com_meta()
    r = atualizar(c, sessao=object(), novas=novas_de("0720"))
    assert r["estado"] == "ok" and r["edicoes_novas"] == ["0720"]
    assert r["julgados_novos"] == 18, r                      # 21 blocos no PDF, 3 repetidos idênticos
    meta = c.read_item("meta", "_meta")
    assert meta["ultima_edicao"] == "0720" and meta["total_edicoes"] == 11 and meta["total_julgados"] == 18
    assert meta["atualizado_em"] > "2020"


def test_rodar_de_novo_nao_duplica():
    c = base_com_meta()
    atualizar(c, sessao=object(), novas=novas_de("0720"))
    atualizar(c, sessao=object(), novas=novas_de("0720"))
    assert sum(1 for (e, _) in c.docs if e == "0720") == 18


def test_reprocessar_edicao_existente_nao_conta_edicao_duas_vezes():
    c = base_com_meta()
    atualizar(c, sessao=object(), novas=novas_de("0720"))
    meta = c.read_item("meta", "_meta")
    meta["ultima_edicao"] = "0719"          # força a base a "esquecer" a 0720, como no teste ponta a ponta
    c.upsert_item(meta)
    atualizar(c, sessao=object(), novas=novas_de("0720"))
    meta = c.read_item("meta", "_meta")
    assert meta["total_edicoes"] == 11 and meta["ultima_edicao"] == "0720" and meta["total_julgados"] == 18


def test_sem_novidade_so_marca_a_verificacao():
    c = base_com_meta()
    r = atualizar(c, sessao=object(), novas=novas_de())
    assert r["estado"] == "sem_novidade"
    meta = c.read_item("meta", "_meta")
    assert meta["ultima_edicao"] == "0719" and meta["ultima_verificacao"] > "2020-01-02"


def test_bloqueio_mantem_o_que_ja_foi_gravado():
    c = base_com_meta()
    r = atualizar(c, sessao=object(), novas=novas_de("0720", bloqueio_depois=True))
    assert r["estado"] == "bloqueio" and r["edicoes_novas"] == ["0720"]
    assert c.read_item("meta", "_meta")["ultima_edicao"] == "0720"


def test_edicao_com_pdf_sem_linha_processo_e_gravada_sem_alerta():
    c = base_com_meta("0799")
    r = atualizar(c, sessao=object(), novas=novas_de("0800"))
    assert r["estado"] == "ok" and r["a_revisar"] == {}, r      # o bloco criado não conta como divergência


def test_base_sem_meta_e_erro_claro():
    r = atualizar(ContainerFalso(), sessao=object(), novas=novas_de())
    assert r["estado"] == "erro" and "carga inicial" in r["mensagem"]


def test_divergencia_de_contagem_marca_a_revisar():
    import atualizar as m
    c = base_com_meta()
    leitor = lambda b: (m.ler_pdf_detalhado(b)[0], 99)           # finge que o PDF tem 99 linhas PROCESSO
    r = atualizar(c, sessao=object(), novas=novas_de("0720"), ler=leitor)
    assert "0720" in r["a_revisar"]
    meta = c.read_item("meta", "_meta")
    assert meta["edicoes_a_revisar"] == ["0720"] and "contagem" in meta["revisar_motivos"]["0720"][0]


if __name__ == "__main__":
    ok = 0
    for nome, f in sorted(globals().items()):
        if nome.startswith("test_"):
            f(); ok += 1; print("ok ", nome)
    print(f"\n{ok} testes passaram")
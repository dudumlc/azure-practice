"""
Testa api/embeddings.py e ingestao/gerar_embeddings.py. Sem Azure, sem Gemini e sem internet.

    python tests/test_embeddings.py
"""
import os
import sys

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "api"))
sys.path.insert(0, os.path.join(RAIZ, "ingestao"))
import embeddings as e  # noqa: E402
import gerar_embeddings as g  # noqa: E402


class Resp:
    def __init__(self, status, corpo=None, texto=""):
        self.status_code, self._corpo, self.text = status, corpo, texto

    def json(self):
        return self._corpo


class GeminiFalso:
    """Responde 200 com um vetor por texto; `falhas` é uma lista de respostas a devolver antes."""
    def __init__(self, falhas=(), limite_chamadas=None):
        self.falhas, self.chamadas, self.limite = list(falhas), 0, limite_chamadas

    def post(self, url, json, headers, timeout):
        assert headers["x-goog-api-key"] == "chave" and url == e.URL
        self.chamadas += 1
        if self.limite is not None and self.chamadas > self.limite:
            return Resp(429, texto="Quota exceeded ... PerDay ...")
        if self.falhas:
            return self.falhas.pop(0)
        return Resp(200, {"embeddings": [{"values": [3.0, 4.0] + [0.0] * (e.DIMENSOES - 2)} for _ in json["requests"]]})


class ContainerFalso:
    def __init__(self, docs):
        self.docs = {(d["edicao"], d["id"]): dict(d) for d in docs}

    def query_items(self, query, enable_cross_partition_query=False):
        assert "NOT IS_DEFINED(c.embedding)" in query
        return [dict(d) for d in self.docs.values() if d["edicao"] != "_meta" and "embedding" not in d]

    def patch_item(self, item, partition_key, patch_operations):
        d = self.docs[(partition_key, item)]
        for op in patch_operations:
            assert op["op"] == "add"
            d[op["path"].lstrip("/")] = op["value"]


def base():
    docs = [{"id": f"d{i}", "edicao": f"09{i:02d}", "tema": f"tema {i}", "destaque": "x"} for i in range(1, 6)]
    docs.append({"id": "vazio", "edicao": "0900", "tema": "", "destaque": ""})                       # sem texto
    docs.append({"id": "pronto", "edicao": "0899", "tema": "a", "destaque": "b", "embedding": [0.1]})  # já tem
    docs.append({"id": "meta", "edicao": "_meta"})
    return ContainerFalso(docs)


def calado(*_):
    pass


def testa_vetor_normalizado():
    v = e.normalizar_vetor([3.0, 4.0])
    assert abs(v[0] - 0.6) < 1e-9 and abs(v[1] - 0.8) < 1e-9


def testa_embutir_em_lotes_e_chama_callback():
    gem, vistos, pausas = GeminiFalso(), [], []
    vets = e.embutir(["a", "b", "c"], "RETRIEVAL_DOCUMENT", "chave", sessao=gem, esperar=pausas.append, progresso=calado,
                     ao_terminar_lote=lambda t, v: vistos.append(len(t)), lote=2, pausa=7)
    assert len(vets) == 3 and vistos == [2, 1] and gem.chamadas == 2 and pausas == [7]


def testa_repete_quando_limite_por_minuto():
    gem, pausas = GeminiFalso(falhas=[Resp(429, texto="rate limit per minute")]), []
    vets = e.embutir(["a"], "RETRIEVAL_QUERY", "chave", sessao=gem, esperar=pausas.append, progresso=calado)
    assert len(vets) == 1 and pausas == [15]


def testa_cota_diaria_levanta():
    try:
        e.embutir(["a"], "RETRIEVAL_QUERY", "chave", sessao=GeminiFalso(limite_chamadas=0), esperar=calado, progresso=calado)
    except e.CotaEsgotada:
        return
    raise AssertionError("devia levantar CotaEsgotada")


def testa_erro_de_chave_levanta():
    try:
        e.embutir(["a"], "RETRIEVAL_QUERY", "chave", sessao=GeminiFalso(falhas=[Resp(400, texto="API key not valid")]),
                  esperar=calado, progresso=calado)
    except e.ErroGemini:
        return
    raise AssertionError("devia levantar ErroGemini")


def testa_poucas_tentativas_para_a_busca_do_site():
    gem = GeminiFalso(falhas=[Resp(429, texto="rate limit per minute")] * 5)
    try:
        e.embutir(["a"], "RETRIEVAL_QUERY", "chave", sessao=gem, esperar=calado, progresso=calado, tentativas=2, timeout=15)
    except e.CotaEsgotada:
        assert gem.chamadas == 2                       # desistiu na 2ª, sem esperar minutos
        return
    raise AssertionError("devia levantar CotaEsgotada")


def testa_gera_so_o_que_falta():
    c = base()
    r = g.gerar(c, "chave", lote=2, pausa=0, sessao=GeminiFalso(), esperar=calado, progresso=calado)
    assert r["pendentes"] == 6 and r["sem_texto"] == 1 and r["nesta_rodada"] == 5 and r["gravados"] == 5 and r["parou_por"] is None, r
    d = c.docs[("0901", "d1")]
    assert d["embedding_modelo"] == e.MODELO and d["embedding_dim"] == 768 and len(d["embedding"]) == 768
    assert abs(d["embedding"][0] - 0.6) < 1e-6 and d["tema"] == "tema 1"          # vetor normalizado; resto intacto
    assert c.docs[("0899", "pronto")]["embedding"] == [0.1]                         # quem já tinha não foi tocado
    assert "embedding" not in c.docs[("0900", "vazio")] and "embedding" not in c.docs[("_meta", "meta")]


def testa_respeita_limite_e_comeca_pelos_mais_novos():
    c = base()
    r = g.gerar(c, "chave", limite=2, lote=50, pausa=0, sessao=GeminiFalso(), esperar=calado, progresso=calado)
    assert r["gravados"] == 2
    assert "embedding" in c.docs[("0905", "d5")] and "embedding" in c.docs[("0904", "d4")]
    assert "embedding" not in c.docs[("0901", "d1")]


def testa_dry_run_nao_chama_nem_grava():
    c, gem = base(), GeminiFalso()
    r = g.gerar(c, "chave", dry_run=True, sessao=gem, esperar=calado, progresso=calado)
    assert r["nesta_rodada"] == 5 and r["gravados"] == 0 and gem.chamadas == 0


def testa_cota_do_dia_para_sem_perder_o_feito():
    c = base()
    r = g.gerar(c, "chave", lote=2, pausa=0, sessao=GeminiFalso(limite_chamadas=1), esperar=calado, progresso=calado)
    assert r["parou_por"] and r["gravados"] == 2                                    # o 1º lote ficou salvo
    r2 = g.gerar(c, "chave", lote=2, pausa=0, sessao=GeminiFalso(), esperar=calado, progresso=calado)
    assert r2["pendentes"] == 4 and r2["gravados"] == 3 and r2["parou_por"] is None  # no dia seguinte continua do que falta


if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("testa_")]
    for t in testes:
        t()
        print("ok:", t.__name__)
    print(f"{len(testes)} testes passaram.")
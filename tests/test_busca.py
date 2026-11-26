"""
Testa api/busca_semantica.py. Sem Azure, sem Gemini e sem internet.

    python tests/test_busca.py
"""
import os
import sys

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "api"))
import busca_semantica as b  # noqa: E402
import embeddings as emb  # noqa: E402
from azure.cosmos import exceptions  # noqa: E402

WHERE, PARAMS = 'c.edicao != "_meta"', []


class Resp:
    def __init__(self, status, corpo=None, texto=""):
        self.status_code, self._corpo, self.text = status, corpo, texto

    def json(self):
        return self._corpo


class GeminiFalso:
    def __init__(self, respostas=None):
        self.respostas, self.chamadas = list(respostas or []), 0

    def post(self, url, json, headers, timeout):
        self.chamadas += 1
        assert timeout == 15 and len(json["requests"]) == 1 and json["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
        if self.respostas:
            return self.respostas.pop(0)
        return Resp(200, {"embeddings": [{"values": [3.0, 4.0] + [0.0] * (emb.DIMENSOES - 2)}]})


class ContainerFalso:
    """Cosmos de mentira com etag. `docs` guarda só o documento de cota; `consultas` registra as buscas."""
    def __init__(self, resultados=()):
        self.docs, self.versao, self.consultas, self.resultados = {}, 0, [], list(resultados)

    def _gravar(self, doc):
        self.versao += 1
        self.docs[doc["id"]] = {**doc, "_etag": f"v{self.versao}"}

    def read_item(self, id, partition_key):
        if id not in self.docs:
            raise exceptions.CosmosResourceNotFoundError(status_code=404, message="não existe")
        return dict(self.docs[id])

    def create_item(self, doc):
        if doc["id"] in self.docs:
            raise exceptions.CosmosHttpResponseError(status_code=409, message="já existe")
        self._gravar(doc)

    def replace_item(self, id, body, etag=None, match_condition=None):
        if self.docs[id]["_etag"] != etag:
            raise exceptions.CosmosHttpResponseError(status_code=412, message="mudou")
        self._gravar(body)

    def patch_item(self, item, partition_key, patch_operations):
        assert patch_operations == [{"op": "incr", "path": "/n", "value": 1}]
        self.docs[item]["n"] += 1

    def query_items(self, query, parameters=None, enable_cross_partition_query=False):
        self.consultas.append((query, parameters))
        return [dict(r) for r in self.resultados]


def cenario(resultados=(), **kw):
    c = ContainerFalso(resultados)
    args = dict(container=c, descricao="plano de saúde que nega o tratamento", where=WHERE, params=PARAMS,
                chave_api="chave", quem="1.2.3.4", limite_pessoa=b.LimitePorPessoa(3, 600), cache=b.CacheDeVetores(),
                teto_dia=5, sessao=GeminiFalso(), esperar=lambda s: None)
    args.update(kw)
    return c, args


def recusada(args, status):
    try:
        b.buscar(**args)
    except b.BuscaRecusada as erro:
        assert erro.status == status, (erro.status, erro.mensagem)
        return erro
    raise AssertionError(f"devia recusar com {status}")


def testa_limite_por_pessoa():
    lim = b.LimitePorPessoa(2, 100)
    assert lim.permitir("a", 0) == (True, 0) and lim.permitir("a", 10) == (True, 0)
    pode, depois = lim.permitir("a", 20)
    assert not pode and depois == 81
    assert lim.permitir("outra", 20)[0]                 # outra pessoa não é afetada
    assert lim.permitir("a", 101)[0]                    # a janela anda: a primeira marca saiu


def testa_cache_descarta_o_mais_antigo():
    c = b.CacheDeVetores(2)
    c.put("a", [1]); c.put("b", [2]); c.get("a"); c.put("c", [3])
    assert c.get("a") == [1] and c.get("b") is None and c.get("c") == [3]


def testa_chave_ignora_acento_caixa_e_espacos():
    assert b.chave_da_descricao("  Plano  de SAÚDE ") == b.chave_da_descricao("plano de saude")


def testa_cota_do_dia_conta_e_bloqueia():
    c = ContainerFalso()
    assert [b.reservar_busca(c, 3, "2026-11-25") for _ in range(4)] == [True, True, True, False]
    assert c.docs["cota_busca"]["n"] == 3


def testa_cota_zera_no_dia_seguinte():
    c = ContainerFalso()
    for _ in range(3):
        b.reservar_busca(c, 3, "2026-11-25")
    assert b.reservar_busca(c, 3, "2026-11-26") and c.docs["cota_busca"] == {**c.docs["cota_busca"], "dia": "2026-11-26", "n": 1}


def testa_cota_aguenta_pedido_simultaneo_na_criacao():
    class Corrida(ContainerFalso):
        def read_item(self, id, partition_key):
            if not self.docs and not getattr(self, "outro", False):      # outro pedido cria entre a leitura e a criação
                self.outro = True
                self._gravar({"id": "cota_busca", "edicao": "_meta", "dia": "2026-11-25", "n": 1})
                raise exceptions.CosmosResourceNotFoundError(status_code=404, message="não existe")
            return super().read_item(id, partition_key)
    c = Corrida()
    assert b.reservar_busca(c, 5, "2026-11-25") and c.docs["cota_busca"]["n"] == 2


def testa_busca_devolve_itens_arredondados():
    c, a = cenario([{"id": "1", "edicao": "0905", "tema": "x", "similaridade": 0.7712345}])
    r = b.buscar(**a)
    assert r == {"itens": [{"id": "1", "edicao": "0905", "tema": "x", "similaridade": 0.7712}]}
    consulta, parametros = c.consultas[0]
    assert "TOP 20" in consulta and "ORDER BY VectorDistance(c.embedding, @v)" in consulta and "IS_DEFINED(c.embedding)" in consulta
    assert parametros[-1]["name"] == "@v" and abs(parametros[-1]["value"][0] - 0.6) < 1e-9     # vetor normalizado


def testa_filtros_vao_na_consulta():
    c, a = cenario(where='c.edicao != "_meta" AND (ARRAY_CONTAINS(c.ramos, @r0))', params=[{"name": "@r0", "value": "DIREITO CIVIL"}])
    b.buscar(**a)
    consulta, parametros = c.consultas[0]
    assert "ARRAY_CONTAINS(c.ramos, @r0)" in consulta and parametros[0] == {"name": "@r0", "value": "DIREITO CIVIL"}


def testa_descricao_curta_ou_longa_ou_sem_chave():
    for texto in ("", "   ", "oi", None):
        _, a = cenario(descricao=texto)
        recusada(a, 400)
    _, a = cenario(descricao="x" * 501)
    recusada(a, 400)
    _, a = cenario(chave_api=None)
    assert recusada(a, 503).mensagem == b.MSG_INDISPONIVEL


def testa_repetida_vem_do_cache_sem_gastar_cota():
    c, a = cenario()
    b.buscar(**a)
    b.buscar(**{**a, "descricao": "  PLANO de saude que nega o tratamento "})
    assert a["sessao"].chamadas == 1 and c.docs["cota_busca"]["n"] == 1


def testa_limite_por_pessoa_na_busca():
    _, a = cenario(limite_pessoa=b.LimitePorPessoa(2, 600))
    for i in range(2):
        b.buscar(**{**a, "descricao": f"descrição bem diferente número {i}"})
    erro = recusada({**a, "descricao": "mais uma descrição nova qualquer"}, 429)
    assert erro.depois_s and "alguns minutos" in erro.mensagem


def testa_teto_do_dia_na_busca():
    _, a = cenario(teto_dia=1)
    b.buscar(**a)
    erro = recusada({**a, "descricao": "outra descrição que ainda não foi buscada"}, 429)
    assert "limite de buscas" in erro.mensagem and a["sessao"].chamadas == 1


def testa_gemini_fora_do_ar_vira_503():
    for resposta in (Resp(429, texto="Quota exceeded PerDay"), Resp(400, texto="API key not valid"), Resp(429, texto="x")):
        _, a = cenario(sessao=GeminiFalso([resposta, resposta]))
        assert recusada(a, 503).mensagem == b.MSG_INDISPONIVEL


def testa_falha_na_contagem_do_dia_vira_503():
    class Quebrado(ContainerFalso):
        def read_item(self, id, partition_key):
            raise RuntimeError("Cosmos fora do ar")
    _, a = cenario(container=Quebrado())
    recusada(a, 503)


if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("testa_")]
    for t in testes:
        t()
        print("ok:", t.__name__)
    print(f"{len(testes)} testes passaram.")

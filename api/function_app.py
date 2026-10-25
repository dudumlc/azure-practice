"""
API do buscador de jurisprudência (Azure Static Web Apps, pasta api/).

Rotas (todas GET):
  /api/meta                       dados da Tela 3 (última edição, total, atualização...)
  /api/ramos                      ramos do direito com a quantidade de julgados de cada um
  /api/julgados                   busca com filtros (lista, sem o inteiro teor)
        ramos=DIREITO CIVIL|DIREITO PENAL   (opcional, vários = OU; separados por "|")
        q=plano de saude                    (opcional, todas as palavras precisam aparecer)
        pagina=1  tamanho=20                (tamanho máximo 50)
  /api/julgados/{edicao}/{id}     um julgado completo, com o inteiro teor
"""
import json
import logging
import os
import time
import unicodedata
from collections import Counter

import azure.functions as func
from azure.cosmos import CosmosClient, exceptions

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

BANCO = os.environ.get("COSMOS_DATABASE", "jurisprudencia")
CONTAINER = os.environ.get("COSMOS_CONTAINER", "julgados")
TAMANHO_MAXIMO = 50
CAMPOS_LISTA = ("c.id, c.edicao, c.processo, c.secao, c.ramos, c.tema, c.destaque, c.link, c.resumo_ia")

_container = None
_cache_ramos = {"quando": 0.0, "dados": None}
CACHE_RAMOS_SEGUNDOS = 3600   # a lista de ramos só muda quando entra edição nova


def container():
    """Cliente criado uma vez por instância da Function (reaproveitado entre chamadas)."""
    global _container
    if _container is None:
        cliente = CosmosClient.from_connection_string(os.environ["COSMOS_CONNECTION_STRING"])
        _container = cliente.get_database_client(BANCO).get_container_client(CONTAINER)
    return _container


def normalizar(texto):
    """minúsculas e sem acento (igual ao tema_busca gravado na carga)."""
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def resposta(corpo, status=200):
    return func.HttpResponse(json.dumps(corpo, ensure_ascii=False), status_code=status,
                             mimetype="application/json", headers={"Cache-Control": "no-store"})


def montar_filtro(ramos, termo):
    """-> (trecho WHERE, parâmetros). Tudo parametrizado: nada do usuário entra no texto da query."""
    condicoes, params = ['c.edicao != "_meta"'], []
    if ramos:
        ors = []
        for i, r in enumerate(ramos):
            ors.append(f"ARRAY_CONTAINS(c.ramos, @r{i})")
            params.append({"name": f"@r{i}", "value": r})
        condicoes.append("(" + " OR ".join(ors) + ")")
    for i, palavra in enumerate(p for p in normalizar(termo).split() if p):
        condicoes.append(f"(CONTAINS(c.tema_busca, @t{i}) OR CONTAINS(c.destaque_busca, @t{i}))")
        params.append({"name": f"@t{i}", "value": palavra})
    return " AND ".join(condicoes), params


def inteiro(valor, padrao, minimo, maximo):
    try:
        return max(minimo, min(maximo, int(valor)))
    except (TypeError, ValueError):
        return padrao


@app.route(route="meta", methods=["GET"])
def meta(req: func.HttpRequest) -> func.HttpResponse:
    try:
        doc = container().read_item("meta", partition_key="_meta")
        return resposta({k: v for k, v in doc.items() if not k.startswith("_")})
    except exceptions.CosmosResourceNotFoundError:
        return resposta({"erro": "base ainda sem dados"}, 404)
    except Exception:
        logging.exception("meta")
        return resposta({"erro": "falha ao consultar a base"}, 500)


@app.route(route="ramos", methods=["GET"])
def ramos(req: func.HttpRequest) -> func.HttpResponse:
    try:
        agora = time.time()
        if _cache_ramos["dados"] is None or agora - _cache_ramos["quando"] > CACHE_RAMOS_SEGUNDOS:
            # GROUP BY entre partições não é aceito pelo SDK: traz só a lista de ramos de cada julgado e conta aqui
            listas = container().query_items('SELECT VALUE c.ramos FROM c WHERE c.edicao != "_meta"',
                                             enable_cross_partition_query=True)
            contagem = Counter(r for lista in listas for r in lista)
            _cache_ramos["dados"] = [{"ramo": r, "total": n}
                                     for r, n in sorted(contagem.items(), key=lambda x: (-x[1], x[0]))]
            _cache_ramos["quando"] = agora
        return resposta(_cache_ramos["dados"])
    except Exception:
        logging.exception("ramos")
        return resposta({"erro": "falha ao consultar a base"}, 500)


@app.route(route="julgados", methods=["GET"])
def julgados(req: func.HttpRequest) -> func.HttpResponse:
    try:
        ramos_sel = [r.strip() for r in req.params.get("ramos", "").split("|") if r.strip()]
        termo = req.params.get("q", "").strip()[:200]
        tamanho = inteiro(req.params.get("tamanho"), 20, 1, TAMANHO_MAXIMO)
        pagina = inteiro(req.params.get("pagina"), 1, 1, 10_000)

        where, params = montar_filtro(ramos_sel, termo)
        c = container()
        total = next(iter(c.query_items(f"SELECT VALUE COUNT(1) FROM c WHERE {where}",
                                        parameters=params, enable_cross_partition_query=True)), 0)
        itens = list(c.query_items(
            f"SELECT {CAMPOS_LISTA} FROM c WHERE {where} ORDER BY c.edicao DESC OFFSET @o LIMIT @l",
            parameters=params + [{"name": "@o", "value": (pagina - 1) * tamanho}, {"name": "@l", "value": tamanho}],
            enable_cross_partition_query=True))
        return resposta({"total": total, "pagina": pagina, "tamanho": tamanho, "itens": itens})
    except Exception:
        logging.exception("julgados")
        return resposta({"erro": "falha ao consultar a base"}, 500)


@app.route(route="julgados/{edicao}/{id}", methods=["GET"])
def julgado(req: func.HttpRequest) -> func.HttpResponse:
    try:
        doc = container().read_item(req.route_params["id"], partition_key=req.route_params["edicao"])
        for campo in [k for k in doc if k.startswith("_")]:
            del doc[campo]
        return resposta(doc)
    except exceptions.CosmosResourceNotFoundError:
        return resposta({"erro": "julgado não encontrado"}, 404)
    except Exception:
        logging.exception("julgado")
        return resposta({"erro": "falha ao consultar a base"}, 500)
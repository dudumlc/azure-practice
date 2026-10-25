"""
API do buscador de jurisprudência (Azure Static Web Apps, pasta api/).

Rotas (todas GET):
  /api/meta                       dados da Tela 3 (última edição, total, atualização...)
  /api/ramos                      ramos do direito com a quantidade de julgados de cada um
  /api/secoes                     seções do informativo (turmas, seções, repetitivos...) com a quantidade de cada uma
  /api/julgados                   busca com filtros (lista, sem o inteiro teor)
        ramos=DIREITO CIVIL|DIREITO PENAL   (opcional, vários = OU; separados por "|")
        secoes=QUARTA TURMA|CORTE ESPECIAL  (opcional, vários = OU; separados por "|")
        q=plano de saude                    (opcional, todas as palavras precisam aparecer)
        pagina=1  tamanho=20                (tamanho máximo 50)
  /api/julgados/{edicao}/{id}     um julgado completo, com o inteiro teor
  POST /api/atualizar             pede uma verificação de edições novas (202 | 409 em andamento | 429 em espera)
  GET  /api/atualizar/status      andamento e resultado da última verificação
"""
import json
import logging
import os
import time
import unicodedata
from collections import Counter

import azure.functions as func
from azure.cosmos import CosmosClient, exceptions

import verificacao

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

BANCO = os.environ.get("COSMOS_DATABASE", "jurisprudencia")
CONTAINER = os.environ.get("COSMOS_CONTAINER", "julgados")
TAMANHO_MAXIMO = 50
CAMPOS_LISTA = ("c.id, c.edicao, c.processo, c.secao, c.ramos, c.tema, c.destaque, c.link, c.resumo_ia")

_container = None
_cache = {}                   # nome -> {"quando": ..., "dados": ...}
CACHE_SEGUNDOS = 3600         # as listas de ramos e seções só mudam quando entra edição nova


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


def montar_filtro(ramos, secoes, termo):
    """-> (trecho WHERE, parâmetros). Tudo parametrizado: nada do usuário entra no texto da query."""
    condicoes, params = ['c.edicao != "_meta"'], []
    if ramos:
        ors = []
        for i, r in enumerate(ramos):
            ors.append(f"ARRAY_CONTAINS(c.ramos, @r{i})")
            params.append({"name": f"@r{i}", "value": r})
        condicoes.append("(" + " OR ".join(ors) + ")")
    if secoes:
        ors = []
        for i, sc in enumerate(secoes):
            ors.append(f"c.secao = @s{i}")
            params.append({"name": f"@s{i}", "value": sc})
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


def contagem_em_cache(nome, consulta, extrair):
    """Conta valores de um campo percorrendo a base uma vez por hora (GROUP BY entre partições não é aceito pelo SDK)."""
    agora = time.time()
    item = _cache.get(nome)
    if item is None or agora - item["quando"] > CACHE_SEGUNDOS:
        valores = container().query_items(consulta, enable_cross_partition_query=True)
        contagem = Counter(v for lista in valores for v in extrair(lista) if v)
        item = {"quando": agora, "dados": [{"nome": k, "total": n} for k, n in sorted(contagem.items())]}
        _cache[nome] = item
    return item["dados"]


@app.route(route="ramos", methods=["GET"])
def ramos(req: func.HttpRequest) -> func.HttpResponse:
    try:
        dados = contagem_em_cache("ramos", 'SELECT VALUE c.ramos FROM c WHERE c.edicao != "_meta"', lambda lista: lista)
        return resposta([{"ramo": d["nome"], "total": d["total"]} for d in dados])
    except Exception:
        logging.exception("ramos")
        return resposta({"erro": "falha ao consultar a base"}, 500)


@app.route(route="secoes", methods=["GET"])
def secoes(req: func.HttpRequest) -> func.HttpResponse:
    try:
        dados = contagem_em_cache("secoes", 'SELECT VALUE c.secao FROM c WHERE c.edicao != "_meta"', lambda v: [v])
        return resposta([{"secao": d["nome"], "total": d["total"]} for d in dados])
    except Exception:
        logging.exception("secoes")
        return resposta({"erro": "falha ao consultar a base"}, 500)


@app.route(route="julgados", methods=["GET"])
def julgados(req: func.HttpRequest) -> func.HttpResponse:
    try:
        ramos_sel = [r.strip() for r in req.params.get("ramos", "").split("|") if r.strip()]
        secoes_sel = [x.strip() for x in req.params.get("secoes", "").split("|") if x.strip()]
        termo = req.params.get("q", "").strip()[:200]
        tamanho = inteiro(req.params.get("tamanho"), 20, 1, TAMANHO_MAXIMO)
        pagina = inteiro(req.params.get("pagina"), 1, 1, 10_000)

        where, params = montar_filtro(ramos_sel, secoes_sel, termo)
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


def enfileirar():
    """Põe a mensagem que dispara a Function de ingestão (fila na Storage Account)."""
    from azure.storage.queue import QueueClient
    fila = QueueClient.from_connection_string(os.environ["STORAGE_CONNECTION_STRING"],
                                              os.environ.get("FILA_ATUALIZACAO", "fila-atualizacao"))
    fila.send_message("verificar")


@app.route(route="atualizar", methods=["POST"])
def atualizar(req: func.HttpRequest) -> func.HttpResponse:
    try:
        status, corpo = verificacao.solicitar_verificacao(container(), enfileirar)
        return resposta(corpo, status)
    except Exception:
        logging.exception("atualizar")
        return resposta({"erro": "não foi possível iniciar a verificação"}, 500)


@app.route(route="atualizar/status", methods=["GET"])
def atualizar_status(req: func.HttpRequest) -> func.HttpResponse:
    try:
        return resposta(verificacao.estado_da_verificacao(container()))
    except Exception:
        logging.exception("atualizar_status")
        return resposta({"erro": "falha ao consultar a base"}, 500)
"""
Busca por descrição: transforma o texto em vetor (Gemini) e pede ao Cosmos os julgados mais parecidos.

Protege a cota do Gemini (cada busca nova gasta 1 requisição do plano gratuito, que tem ~1.000 por dia):
- limite por pessoa (em memória, por instância da API);
- teto diário global, contado em um documento do Cosmos (`cota_busca`, partição `_meta`);
- cache das últimas descrições já vetorizadas (a mesma busca repetida não gasta cota).
"""
import time
import unicodedata
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone

import requests
from azure.core import MatchConditions
from azure.cosmos import exceptions

from embeddings import CotaEsgotada, ErroGemini, embutir

TOP = 20
MIN_CHARS = 8
MAX_CHARS = 500
BUSCAS_POR_PESSOA = 15
JANELA_PESSOA_S = 600
BUSCAS_POR_DIA = 700                                 # sobra folga da cota de ~1.000/dia para a atualização semanal
DIA_GEMINI = timezone(timedelta(hours=-8))           # a cota do Gemini zera à meia-noite do Pacífico
ID_COTA, PARTICAO_COTA = "cota_busca", "_meta"
CAMPOS = "c.id, c.edicao, c.processo, c.secao, c.ramos, c.tema, c.destaque, c.link, c.resumo_ia"
MSG_INDISPONIVEL = "A busca por descrição está indisponível no momento. Use a busca por filtros enquanto isso."


class BuscaRecusada(Exception):
    """Pedido que não vira resultado: `status` é o código HTTP e `mensagem` o texto para a tela."""
    def __init__(self, mensagem, status, depois_s=None):
        super().__init__(mensagem)
        self.mensagem, self.status, self.depois_s = mensagem, status, depois_s


class LimitePorPessoa:
    def __init__(self, maximo=BUSCAS_POR_PESSOA, janela_s=JANELA_PESSOA_S):
        self.maximo, self.janela, self._marcas = maximo, janela_s, {}

    def permitir(self, chave, agora):
        """-> (pode, segundos_ate_poder)."""
        marcas = self._marcas.setdefault(chave, deque())
        while marcas and agora - marcas[0] >= self.janela:
            marcas.popleft()
        if len(marcas) >= self.maximo:
            return False, int(self.janela - (agora - marcas[0])) + 1
        marcas.append(agora)
        if len(self._marcas) > 5000:                          # não deixa a memória crescer sem fim
            self._marcas = {k: v for k, v in self._marcas.items() if v}
        return True, 0


class CacheDeVetores:
    def __init__(self, maximo=200):
        self.maximo, self._itens = maximo, OrderedDict()

    def get(self, chave):
        if chave in self._itens:
            self._itens.move_to_end(chave)
            return self._itens[chave]
        return None

    def put(self, chave, vetor):
        self._itens[chave] = vetor
        self._itens.move_to_end(chave)
        while len(self._itens) > self.maximo:
            self._itens.popitem(last=False)


def chave_da_descricao(texto):
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    return " ".join(sem_acento.split())


def dia_de_hoje(momento=None):
    return (momento or datetime.now(timezone.utc)).astimezone(DIA_GEMINI).strftime("%Y-%m-%d")


def reservar_busca(container, teto, hoje):
    """True se ainda cabe uma busca nova hoje (e já a conta). Vários pedidos ao mesmo tempo não se atropelam:
    a criação falha se o documento já existe e a virada de dia só vale se ninguém mexeu no documento."""
    for _ in range(4):
        try:
            doc = container.read_item(ID_COTA, partition_key=PARTICAO_COTA)
        except exceptions.CosmosResourceNotFoundError:
            try:
                container.create_item({"id": ID_COTA, "edicao": PARTICAO_COTA, "dia": hoje, "n": 1})
                return True
            except exceptions.CosmosHttpResponseError as erro:
                if erro.status_code == 409:
                    continue
                raise
        if doc.get("dia") != hoje:
            novo = {"id": ID_COTA, "edicao": PARTICAO_COTA, "dia": hoje, "n": 1}
            try:
                container.replace_item(ID_COTA, novo, etag=doc["_etag"], match_condition=MatchConditions.IfNotModified)
                return True
            except exceptions.CosmosHttpResponseError as erro:
                if erro.status_code in (409, 412):
                    continue
                raise
        if doc.get("n", 0) >= teto:
            return False
        container.patch_item(ID_COTA, PARTICAO_COTA, [{"op": "incr", "path": "/n", "value": 1}])
        return True
    raise RuntimeError("não foi possível contar a busca do dia")


def consultar(container, vetor, where, params, top=TOP):
    consulta = (f"SELECT TOP {int(top)} {CAMPOS}, VectorDistance(c.embedding, @v) AS similaridade FROM c "
                f"WHERE {where} AND IS_DEFINED(c.embedding) ORDER BY VectorDistance(c.embedding, @v)")
    itens = list(container.query_items(consulta, parameters=list(params) + [{"name": "@v", "value": vetor}],
                                       enable_cross_partition_query=True))
    for it in itens:
        it["similaridade"] = round(it["similaridade"], 4)
    return itens


def buscar(container, descricao, where, params, chave_api, quem, limite_pessoa, cache,
           teto_dia=BUSCAS_POR_DIA, agora=time.time, momento=None, sessao=requests, esperar=time.sleep):
    """-> {"itens": [...]} (do mais parecido para o menos). Levanta BuscaRecusada com o status e a mensagem."""
    texto = " ".join(str(descricao or "").split())
    if len(texto) < MIN_CHARS:
        raise BuscaRecusada(f"Descreva com um pouco mais de detalhe (pelo menos {MIN_CHARS} caracteres).", 400)
    if len(texto) > MAX_CHARS:
        raise BuscaRecusada(f"A descrição passou de {MAX_CHARS} caracteres. Resuma um pouco.", 400)
    if not chave_api:
        raise BuscaRecusada(MSG_INDISPONIVEL, 503)

    chave = chave_da_descricao(texto)
    vetor = cache.get(chave)
    if vetor is None:
        pode, depois = limite_pessoa.permitir(quem, agora())
        if not pode:
            raise BuscaRecusada("Muitas buscas em pouco tempo. Tente de novo em alguns minutos.", 429, depois)
        try:
            cabe = reservar_busca(container, teto_dia, dia_de_hoje(momento))
        except Exception:
            raise BuscaRecusada(MSG_INDISPONIVEL, 503)
        if not cabe:
            raise BuscaRecusada("O limite de buscas por descrição de hoje foi atingido. Use a busca por filtros e "
                                "tente de novo amanhã.", 429)
        try:
            vetor = embutir([texto], "RETRIEVAL_QUERY", chave_api, sessao=sessao, esperar=lambda s: esperar(min(s, 2)),
                            progresso=lambda *_: None, tentativas=2, timeout=15)[0]
        except (CotaEsgotada, ErroGemini, requests.RequestException):
            raise BuscaRecusada(MSG_INDISPONIVEL, 503)
        cache.put(chave, vetor)
    return {"itens": consultar(container, vetor, where, params)}

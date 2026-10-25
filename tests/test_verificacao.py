"""
Testa api/verificacao.py (regras do botão "Verificar agora"). Sem Azure e sem internet.

    python tests/test_verificacao.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
from azure.cosmos import exceptions  # noqa: E402
from verificacao import iso, solicitar_verificacao, estado_da_verificacao  # noqa: E402

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class ContainerFalso:
    """Cosmos de mentira com etag, como o de verdade: replace_item recusa (412) se o documento mudou."""
    def __init__(self):
        self.docs, self.versao = {}, 0

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

    def upsert_item(self, doc):
        self._gravar(doc)


class Fila:
    def __init__(self, falha=False):
        self.mensagens, self.falha = [], falha

    def __call__(self):
        if self.falha:
            raise RuntimeError("fila fora do ar")
        self.mensagens.append("verificar")


def job_pronto(c, **campos):
    c._gravar({"id": "job", "edicao": "_meta", **campos})


def test_primeira_vez_enfileira():
    c, fila = ContainerFalso(), Fila()
    status, corpo = solicitar_verificacao(c, fila, T0)
    assert status == 202 and corpo["estado"] == "na_fila" and fila.mensagens == ["verificar"]
    assert c.read_item("job", "_meta")["estado"] == "na_fila"


def test_dentro_da_espera_de_7_dias_recusa_e_nao_enfileira():
    c, fila = ContainerFalso(), Fila()
    job_pronto(c, estado="ok", proxima_verificacao_em=iso(T0 + timedelta(days=5)))
    status, corpo = solicitar_verificacao(c, fila, T0)
    assert status == 429 and corpo["motivo"] == "espera" and not corpo["pode_verificar"] and not fila.mensagens
    assert corpo["proxima_verificacao_em"] == iso(T0 + timedelta(days=5))


def test_depois_da_espera_libera():
    c, fila = ContainerFalso(), Fila()
    job_pronto(c, estado="ok", proxima_verificacao_em=iso(T0 - timedelta(minutes=1)))
    assert solicitar_verificacao(c, fila, T0)[0] == 202 and len(fila.mensagens) == 1


def test_ja_na_fila_ou_rodando_recusa():
    for estado in ("na_fila", "rodando"):
        c, fila = ContainerFalso(), Fila()
        job_pronto(c, estado=estado, criado_em=iso(T0 - timedelta(minutes=2)), iniciado_em=iso(T0 - timedelta(minutes=1)))
        status, corpo = solicitar_verificacao(c, fila, T0)
        assert status == 409 and corpo["motivo"] == "em_andamento" and not fila.mensagens


def test_job_travado_ha_muito_tempo_libera():
    c, fila = ContainerFalso(), Fila()
    job_pronto(c, estado="rodando", iniciado_em=iso(T0 - timedelta(minutes=40)))
    assert solicitar_verificacao(c, fila, T0)[0] == 202


def test_dois_cliques_ao_mesmo_tempo_so_um_enfileira():
    c, fila = ContainerFalso(), Fila()
    job_pronto(c, estado="ok", proxima_verificacao_em=iso(T0 - timedelta(hours=1)))
    lido = c.read_item("job", "_meta")
    original = c.read_item
    chamadas = []

    def leitura_atrasada(id, partition_key):          # o 2º pedido leu o job antes do 1º gravar
        chamadas.append(1)
        return lido if len(chamadas) == 2 else original(id, partition_key)

    assert solicitar_verificacao(c, fila, T0)[0] == 202
    c.read_item = leitura_atrasada
    chamadas.clear(); chamadas.append(1)
    status, _ = solicitar_verificacao(c, fila, T0)
    assert status == 409 and len(fila.mensagens) == 1


def test_falha_ao_enfileirar_nao_prende_o_botao():
    c = ContainerFalso()
    try:
        solicitar_verificacao(c, Fila(falha=True), T0)
        assert False
    except RuntimeError:
        pass
    job = c.read_item("job", "_meta")
    assert job["estado"] == "erro"
    assert solicitar_verificacao(c, Fila(), T0 + timedelta(seconds=1))[0] == 202


def test_status_sem_job_libera_o_botao():
    s = estado_da_verificacao(ContainerFalso(), T0)
    assert s["estado"] == "nunca" and s["pode_verificar"]


def test_status_nao_vaza_campos_internos():
    c = ContainerFalso()
    job_pronto(c, estado="ok", mensagem="1 edição nova", proxima_verificacao_em=iso(T0 + timedelta(days=7)))
    s = estado_da_verificacao(c, T0)
    assert not any(k.startswith("_") for k in s) and s["mensagem"] == "1 edição nova"


if __name__ == "__main__":
    ok = 0
    for nome, f in sorted(globals().items()):
        if nome.startswith("test_"):
            f(); ok += 1; print("ok ", nome)
    print(f"\n{ok} testes passaram")
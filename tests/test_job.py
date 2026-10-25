"""
Testa ingestao/job.py (estado do job e espera entre verificações). Sem Azure e sem internet.

    python tests/test_job.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingestao"))
from job import ESPERA_COM_NOVIDADE, ESPERA_SEM_NOVIDADE, executar_job, ler_job  # noqa: E402
from test_atualizar import ContainerFalso  # noqa: E402

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def rodar_ok(container, progresso=None):
    progresso("Edição 0900: lendo o PDF…")
    return {"estado": "ok", "mensagem": "1 edição nova", "edicoes_novas": ["0900"], "julgados_novos": 17, "a_revisar": {}}


def rodar_sem_novidade(container, progresso=None):
    return {"estado": "sem_novidade", "mensagem": "Nada novo", "edicoes_novas": [], "julgados_novos": 0, "a_revisar": {}}


def rodar_quebrado(container, progresso=None):
    raise RuntimeError("falhou de propósito")


def test_com_novidade_espera_7_dias():
    c = ContainerFalso()
    j = executar_job(c, rodar=rodar_ok, relogio=lambda: T0)
    assert j["estado"] == "ok" and j["resultado"]["julgados_novos"] == 17
    assert datetime.fromisoformat(j["proxima_verificacao_em"]) == T0 + ESPERA_COM_NOVIDADE
    assert ler_job(c)["estado"] == "ok"


def test_sem_novidade_espera_1_hora():
    j = executar_job(ContainerFalso(), rodar=rodar_sem_novidade, relogio=lambda: T0)
    assert datetime.fromisoformat(j["proxima_verificacao_em"]) == T0 + ESPERA_SEM_NOVIDADE


def test_falha_vira_erro_e_nao_deixa_job_preso():
    c = ContainerFalso()
    j = executar_job(c, rodar=rodar_quebrado, relogio=lambda: T0)
    assert j["estado"] == "erro" and "falhou de propósito" in j["mensagem"]
    assert ler_job(c)["estado"] == "erro"


def test_mensagem_repetida_nao_roda_duas_vezes():
    c = ContainerFalso()
    c.upsert_item({"id": "job", "edicao": "_meta", "estado": "rodando", "iniciado_em": (T0 - timedelta(minutes=2)).isoformat()})
    chamadas = []
    j = executar_job(c, rodar=lambda *a, **k: chamadas.append(1), relogio=lambda: T0)
    assert j["estado"] == "rodando" and not chamadas


def test_job_rodando_ha_muito_tempo_e_retomado():
    c = ContainerFalso()
    c.upsert_item({"id": "job", "edicao": "_meta", "estado": "rodando", "iniciado_em": (T0 - timedelta(minutes=30)).isoformat()})
    assert executar_job(c, rodar=rodar_sem_novidade, relogio=lambda: T0)["estado"] == "sem_novidade"


if __name__ == "__main__":
    ok = 0
    for nome, f in sorted(globals().items()):
        if nome.startswith("test_"):
            f(); ok += 1; print("ok ", nome)
    print(f"\n{ok} testes passaram")
"""
Testa api/job.py (estado do job e espera entre verificações). Sem Azure e sem internet.

    python tests/test_job.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
from job import BRT, ESPERA_PARCIAL, proxima_publicacao, ESPERA_SEM_NOVIDADE, TRAVA_RODANDO, executar_job, ler_job  # noqa: E402
from test_atualizar import ContainerFalso  # noqa: E402

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def rodar_ok(container, progresso=None):
    progresso("Edição 0900: lendo o PDF…")
    return {"estado": "ok", "mensagem": "1 edição nova", "edicoes_novas": ["0900"], "julgados_novos": 17, "a_revisar": {}}


def rodar_sem_novidade(container, progresso=None):
    return {"estado": "sem_novidade", "mensagem": "Nada novo", "edicoes_novas": [], "julgados_novos": 0, "a_revisar": {}}


def rodar_parcial(container, progresso=None, orcamento_s=None):
    return {"estado": "parcial", "mensagem": "Gravei 0900; aperte de novo.", "edicoes_novas": ["0900"],
            "julgados_novos": 17, "a_revisar": {}}


def rodar_quebrado(container, progresso=None):
    raise RuntimeError("falhou de propósito")


def test_com_novidade_trava_ate_a_proxima_terca():
    assert T0.weekday() == 5                                     # 24/10/2026 é sábado

    c = ContainerFalso()
    j = executar_job(c, rodar=rodar_ok, relogio=lambda: T0)
    assert j["estado"] == "ok" and j["resultado"]["julgados_novos"] == 17
    assert datetime.fromisoformat(j["proxima_verificacao_em"]) == datetime(2026, 10, 27, 0, 0, tzinfo=BRT)
    assert ler_job(c)["estado"] == "ok"


def test_proxima_publicacao_em_cada_dia_da_semana():
    esperado = {0: "2026-10-27", 1: "2026-11-03", 2: "2026-11-03", 3: "2026-11-03", 4: "2026-11-03",
                5: "2026-11-03", 6: "2026-11-03"}
    segunda = datetime(2026, 10, 26, 15, 0, tzinfo=BRT)         # segunda-feira
    for i in range(7):
        dia = segunda + timedelta(days=i)
        assert proxima_publicacao(dia).date().isoformat() == esperado[i], (i, proxima_publicacao(dia))
    assert proxima_publicacao(datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc)).date().isoformat() == "2026-11-03"


def test_sem_novidade_espera_1_hora():
    j = executar_job(ContainerFalso(), rodar=rodar_sem_novidade, relogio=lambda: T0)
    assert datetime.fromisoformat(j["proxima_verificacao_em"]) == T0 + ESPERA_SEM_NOVIDADE


def test_parcial_libera_o_botao_na_hora():
    c = ContainerFalso()
    j = executar_job(c, rodar=rodar_parcial, relogio=lambda: T0, orcamento_s=35)
    assert j["estado"] == "parcial" and j["resultado"]["edicoes_novas"] == ["0900"]
    assert datetime.fromisoformat(j["proxima_verificacao_em"]) == T0 + ESPERA_PARCIAL == T0


def test_orcamento_e_repassado_so_quando_informado():
    recebidos = []

    def rodar(container, progresso=None, **kw):
        recebidos.append(kw)
        return {"estado": "sem_novidade", "mensagem": "", "edicoes_novas": [], "julgados_novos": 0, "a_revisar": {}}

    executar_job(ContainerFalso(), rodar=rodar, relogio=lambda: T0)
    executar_job(ContainerFalso(), rodar=rodar, relogio=lambda: T0, orcamento_s=35)
    assert recebidos == [{}, {"orcamento_s": 35}]


def test_falha_vira_erro_e_nao_deixa_job_preso():
    c = ContainerFalso()
    j = executar_job(c, rodar=rodar_quebrado, relogio=lambda: T0)
    assert j["estado"] == "erro" and "falhou de propósito" in j["mensagem"]
    assert ler_job(c)["estado"] == "erro"


def test_mensagem_repetida_nao_roda_duas_vezes():
    c = ContainerFalso()
    c.upsert_item({"id": "job", "edicao": "_meta", "estado": "rodando", "iniciado_em": (T0 - timedelta(seconds=40)).isoformat()})
    chamadas = []
    j = executar_job(c, rodar=lambda *a, **k: chamadas.append(1), relogio=lambda: T0)
    assert j["estado"] == "rodando" and not chamadas


def test_job_rodando_ha_muito_tempo_e_retomado():
    c = ContainerFalso()
    c.upsert_item({"id": "job", "edicao": "_meta", "estado": "rodando", "iniciado_em": (T0 - TRAVA_RODANDO - timedelta(minutes=1)).isoformat()})
    assert executar_job(c, rodar=rodar_sem_novidade, relogio=lambda: T0)["estado"] == "sem_novidade"


if __name__ == "__main__":
    ok = 0
    for nome, f in sorted(globals().items()):
        if nome.startswith("test_"):
            f(); ok += 1; print("ok ", nome)
    print(f"\n{ok} testes passaram")
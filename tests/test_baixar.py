"""
Testa o prazo de api/baixar_informativos.py (esperas e novas tentativas respeitam o tempo da chamada). Sem internet.

    python tests/test_baixar.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
import baixar_informativos as b  # noqa: E402


class Resposta:
    def __init__(self, status, conteudo=b"", headers=None):
        self.status_code, self.content, self.headers = status, conteudo, headers or {}


class SessaoFalsa:
    def __init__(self, *respostas):
        self.respostas, self.timeouts = list(respostas), []

    def get(self, url, timeout=None):
        self.timeouts.append(timeout)
        return self.respostas.pop(0)


class SemEsperar:
    """Troca time.sleep por um registro, para o teste não demorar."""
    def __enter__(self):
        self.pausas, self.original = [], time.sleep
        time.sleep = self.pausas.append
        return self

    def __exit__(self, *a):
        time.sleep = self.original


def test_sem_prazo_tenta_3_vezes_e_nao_espera_depois_da_ultima():
    with SemEsperar() as e:
        sessao = SessaoFalsa(Resposta(503), Resposta(503), Resposta(503))
        assert b.baixar_pdf(sessao, "u")[0] == "bloqueio"
    assert e.pausas == [5, 10]                       # antes da 2ª e da 3ª tentativa; depois da 3ª não espera


def test_com_prazo_curto_nao_espera_para_tentar_de_novo():
    with SemEsperar() as e:
        sessao = SessaoFalsa(Resposta(503), Resposta(200, b"%PDF"))
        status, _ = b.baixar_pdf(sessao, "u", prazo=time.monotonic() + 8)    # a espera de 5 s cabe no prazo de 8 s
    assert status == "ok" and e.pausas == [5]
    with SemEsperar() as e:
        sessao = SessaoFalsa(Resposta(503), Resposta(200, b"%PDF"))
        status, _ = b.baixar_pdf(sessao, "u", prazo=time.monotonic() + 4)    # 5 s de espera não cabe: desiste
    assert status == "tempo" and e.pausas == [] and len(sessao.timeouts) == 1


def test_prazo_ja_vencido_nem_tenta():
    sessao = SessaoFalsa()
    assert b.baixar_pdf(sessao, "u", prazo=time.monotonic() + 1)[0] == "tempo" and not sessao.timeouts


def test_timeout_da_requisicao_nunca_passa_do_prazo():
    sessao = SessaoFalsa(Resposta(200, b"%PDF"))
    b.baixar_pdf(sessao, "u", prazo=time.monotonic() + 20)
    assert 3 <= sessao.timeouts[0] <= 20


def test_novas_edicoes_levanta_tempo_esgotado():
    sessao = SessaoFalsa(Resposta(503))
    try:
        with SemEsperar():
            list(b.novas_edicoes(sessao, 905, prazo=time.monotonic() + 4))
        assert False
    except b.TempoEsgotado:
        pass


def test_novas_edicoes_sem_prazo_continua_igual():
    sessao = SessaoFalsa(Resposta(200, b"%PDF-1"), Resposta(404), Resposta(404), Resposta(404))
    with SemEsperar():
        assert [e for e, _ in b.novas_edicoes(sessao, 905)] == ["0906"]


if __name__ == "__main__":
    ok = 0
    for nome, f in sorted(globals().items()):
        if nome.startswith("test_"):
            f(); ok += 1; print("ok ", nome)
    print(f"\n{ok} testes passaram")
"""
Testa o campo `tipo`: novos documentos (registro_julgado.montar_docs) e o script ingestao/adicionar_tipo.py.
Sem Azure e sem internet.

    python tests/test_tipo.py
"""
import os
import sys

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "api"))
sys.path.insert(0, os.path.join(RAIZ, "ingestao"))
from adicionar_tipo import adicionar_tipo  # noqa: E402
from registro_julgado import TIPO_INFORMATIVO, montar_docs  # noqa: E402


class ContainerFalso:
    """Só o que adicionar_tipo.py usa: query_items e patch_item."""
    def __init__(self, docs):
        self.docs = {(d["edicao"], d["id"]): dict(d) for d in docs}
        self.patches = 0

    def query_items(self, query, enable_cross_partition_query=False):
        assert "NOT IS_DEFINED(c.tipo)" in query and '!= "_meta"' in query
        return [{"id": i, "edicao": e} for (e, i), d in self.docs.items() if e != "_meta" and "tipo" not in d]

    def patch_item(self, item, partition_key, patch_operations):
        assert len(patch_operations) == 1 and patch_operations[0]["op"] == "add" and patch_operations[0]["path"] == "/tipo"
        self.docs[(partition_key, item)]["tipo"] = patch_operations[0]["value"]
        self.patches += 1


def base():
    return ContainerFalso([
        {"id": "a1", "edicao": "0905", "tema": "x"},
        {"id": "a2", "edicao": "0905", "tema": "y"},
        {"id": "b1", "edicao": "0906", "tema": "z", "tipo": "informativo-stj"},     # já tem: não mexe
        {"id": "meta", "edicao": "_meta", "ultima_edicao": "0906"},
        {"id": "job", "edicao": "_meta", "estado": "ok"},
    ])


def test_documentos_novos_ja_nascem_com_tipo():
    bloco = {"PROCESSO": "REsp 1.234.567/SP", "SECAO": "TERCEIRA TURMA", "RAMO_DO_DIREITO": "DIREITO CIVIL", "TEMA": "Tema",
             "DESTAQUE": "Destaque", "INTEIRO_TEOR": "Texto", "OBS": ""}
    docs = montar_docs("0906", [bloco])
    assert docs and all(d["tipo"] == TIPO_INFORMATIVO == "informativo-stj" for d in docs)


def test_dry_run_so_conta():
    c = base()
    assert adicionar_tipo(c, dry_run=True, progresso=lambda t: None) == 2 and c.patches == 0


def test_acrescenta_so_nos_que_faltam_e_nao_toca_meta_nem_job():
    c = base()
    assert adicionar_tipo(c, progresso=lambda t: None) == 2
    assert c.docs[("0905", "a1")] == {"id": "a1", "edicao": "0905", "tema": "x", "tipo": "informativo-stj"}
    assert "tipo" not in c.docs[("_meta", "meta")] and "tipo" not in c.docs[("_meta", "job")]


def test_rodar_de_novo_nao_faz_nada():
    c = base()
    adicionar_tipo(c, progresso=lambda t: None)
    assert adicionar_tipo(c, progresso=lambda t: None) == 0 and c.patches == 2


if __name__ == "__main__":
    ok = 0
    for nome, f in sorted(globals().items()):
        if nome.startswith("test_"):
            f(); ok += 1; print("ok ", nome)
    print(f"\n{ok} testes passaram")
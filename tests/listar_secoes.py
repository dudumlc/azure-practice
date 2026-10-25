"""
Descobre as seções dos informativos (CORTE ESPECIAL, QUARTA TURMA, RECURSOS REPETITIVOS...) em todos os PDFs.

Uso (a partir da raiz do projeto):
    python tests/listar_secoes.py CAMINHO_DA_PASTA_DE_PDFS

Para cada linha "PROCESSO ..." olha o que vem logo antes dela: as linhas em caixa alta que não são
marcadores de campo são o título da seção. Mostra cada título com a quantidade de processos e as edições
onde aparece, e compara a seção com o órgão julgador escrito no próprio processo (Quarta Turma etc.).
Grava secoes_encontradas.csv com um processo por linha.
"""
import csv
import os
import re
import sys
from collections import Counter, defaultdict

import pdfplumber

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingestao"))
from extrator_informativos import FIM_DE_BLOCO, _linhas_limpas  # noqa: E402

RE_CAIXA_ALTA = re.compile(r"^[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ0-9 ,\-–/ªº.]{3,}$")
CAMPOS = ("PROCESSO", "RAMO DO DIREITO", "TEMA", "DESTAQUE", "INFORMAÇÕES DO INTEIRO TEOR")
RE_ORGAO = re.compile(r"(Corte Especial|(?:Primeira|Segunda|Terceira|Quarta|Quinta|Sexta) (?:Turma|Seção))")


def titulo_antes(linhas, i):
    """Linhas em caixa alta coladas logo acima do PROCESSO (até 3), na ordem em que aparecem."""
    achadas = []
    for j in range(i - 1, max(i - 4, -1), -1):
        l = linhas[j]
        if RE_CAIXA_ALTA.match(l) and l not in FIM_DE_BLOCO and not l.startswith(CAMPOS):
            achadas.insert(0, l)
        else:
            break
    return " / ".join(achadas)


def main(pasta):
    linhas_csv, por_titulo, edicoes = [], Counter(), defaultdict(set)
    pdfs = sorted(f for f in os.listdir(pasta) if f.lower().endswith(".pdf"))
    for n, f in enumerate(pdfs, 1):
        with pdfplumber.open(os.path.join(pasta, f)) as pdf:
            linhas = _linhas_limpas([p.extract_text() for p in pdf.pages])
        for i, l in enumerate(linhas):
            if not l.startswith("PROCESSO "):
                continue
            t = titulo_antes(linhas, i)
            ger = " ".join(linhas[i:i + 3])
            orgao = (RE_ORGAO.search(ger) or [None, ""])[1] if RE_ORGAO.search(ger) else ""
            por_titulo[t or "(sem título logo acima)"] += 1
            edicoes[t or "(sem título logo acima)"].add(f[3:-4])
            linhas_csv.append({"edicao": f[3:-4], "titulo_acima": t, "orgao_no_processo": orgao, "processo": l[9:100]})
        if n % 50 == 0:
            print(f"  ... {n}/{len(pdfs)} PDFs lidos")

    print(f"\n{len(linhas_csv)} processos em {len(pdfs)} PDFs. Títulos encontrados logo acima de um PROCESSO:\n")
    for t, q in por_titulo.most_common():
        ex = sorted(edicoes[t])
        print(f"{q:5}  {t}   (em {len(ex)} edições, ex.: {', '.join(ex[:4])})")
    with open("secoes_encontradas.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["edicao", "titulo_acima", "orgao_no_processo", "processo"])
        w.writeheader()
        w.writerows(linhas_csv)
    print("\nDetalhe por processo em secoes_encontradas.csv")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
"""
Teste rápido do extrator em poucas edições, sem Excel e sem rodar os 269 PDFs.

Uso (a partir da raiz do projeto):
    python tests/testar_edicoes.py CAMINHO_DA_PASTA_DE_PDFS 0683 0686 0693
    python tests/testar_edicoes.py CAMINHO_DA_PASTA_DE_PDFS --csv relatorio_validacao.csv

Para cada edição compara quantos "PROCESSO" existem no PDF com quantos blocos o extrator achou
e mostra o que ainda parece errado. Também grava o texto dos blocos em teste_edicoes.txt para conferência.
"""
import os
import sys

import pdfplumber

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingestao"))
from extrator_informativos import ler_pdf  # noqa: E402


def contar_no_pdf(caminho):
    with pdfplumber.open(caminho) as pdf:
        linhas = [l.strip() for p in pdf.pages for l in (p.extract_text() or "").splitlines()]
    return {
        "PROCESSO": sum(1 for l in linhas if l.startswith("PROCESSO ")),
        "DESTAQUE": sum(1 for l in linhas if l == "DESTAQUE"),
        "TEOR": sum(1 for l in linhas if l == "INFORMAÇÕES DO INTEIRO TEOR"),
    }


def edicoes_do_csv(caminho):
    with open(caminho, encoding="utf-8") as fh:
        linhas = fh.read().splitlines()[1:]
    return sorted({l.split(",")[0][3:-4] for l in linhas if l.startswith("Inf")})


def main(pasta, edicoes):
    saida = []
    ruins = 0
    print(f"{'edição':8} {'PDF':>4} {'blocos':>6}  problemas")
    for ed in edicoes:
        caminho = os.path.join(pasta, f"Inf{ed}.pdf")
        if not os.path.exists(caminho):
            print(f"{ed:8} PDF não encontrado em {pasta}")
            continue
        no_pdf = contar_no_pdf(caminho)
        blocos = ler_pdf(caminho)
        problemas = []
        ausentes = sum("PROCESSO ausente no PDF" in b["OBS"] for b in blocos)
        if len(blocos) - ausentes != no_pdf["PROCESSO"]:
            problemas.append(f"contagem: PDF {no_pdf['PROCESSO']} x extraídos {len(blocos)}")
        if ausentes:
            problemas.append(f"{ausentes} afetação sem linha PROCESSO no PDF (esperado, bloco criado)")
        n = sum("RAMO DO DIREITO" in b["TEMA"] for b in blocos)
        if n:
            problemas.append(f"{n} TEMA com 'RAMO DO DIREITO' dentro")
        n = sum("fora de ordem" in b["OBS"] for b in blocos)
        if n:
            problemas.append(f"{n} com marcador fora de ordem")
        n = sum(b["TEMA"] == "" for b in blocos)
        if n:
            problemas.append(f"{n} sem TEMA")
        n = sum(b["RAMO_DO_DIREITO"] == "" for b in blocos)
        if n:
            problemas.append(f"{n} sem RAMO (avisos ou julgados sem ramo)")
        ruins += bool([p for p in problemas if p.startswith(("contagem", "1 TEMA", "TEMA")) or "RAMO DO DIREITO" in p])
        print(f"{ed:8} {no_pdf['PROCESSO']:>4} {len(blocos):>6}  " + ("; ".join(problemas) or "ok"))
        for b in blocos:
            saida.append(f"##### {ed} | {b['PROCESSO'][:90]}\nRAMO: {b['RAMO_DO_DIREITO']}\nOBS: {b['OBS']}\n"
                         f"TEMA: {b['TEMA'][:400]}\nDESTAQUE: {b['DESTAQUE'][:200]}\nTEOR: {b['INTEIRO_TEOR'][:200]}\n")
    with open("teste_edicoes.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(saida))
    print("\nTexto dos blocos em teste_edicoes.txt")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    pasta, resto = sys.argv[1], sys.argv[2:]
    main(pasta, edicoes_do_csv(resto[1]) if resto[0] == "--csv" else resto)
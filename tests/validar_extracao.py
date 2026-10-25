"""
Confere o Excel gerado pelo extrator contra os PDFs de origem.

Uso:
    python tests/validar_extracao.py data outputs/jurisprudencia_extraida.xlsx            (todas as edições)
    python tests/validar_extracao.py data outputs/jurisprudencia_extraida.xlsx 0818 0834  (só essas)

Para cada PDF, conta no próprio texto quantos blocos "PROCESSO", "DESTAQUE" e
"INFORMAÇÕES DO INTEIRO TEOR" existem e compara com o que foi para o Excel.
Também procura lixo de página (rodapé/cabeçalho) e campos vazios nas linhas extraídas.
Só imprime o que está errado; as divergências por edição vão para relatorio_validacao.csv.
"""
import os
import re
import sys

import pandas as pd
import pdfplumber

CHAVE = ["ARQUIVO", "PROCESSO", "SECAO", "TEMA", "DESTAQUE", "INTEIRO_TEOR"]   # identifica um julgado (o Excel repete por ramo)
LIXO = [r"processo\.stj\.jus\.br", r"Informativo de Jurisprudência n\.", r"Este periódico destaca",
        r"INFORMAÇÕES ADICIONAIS", r"INFORMAÇÕES DO INTEIRO TEOR", r"\bSAIBA MAIS\b", r"ÁUDIO DO TEXTO",
        r"VÍDEO DO JULGAMENTO", r"RAMO DO DIREITO"]
OBS_ESPERADA = {"sem DESTAQUE; sem INTEIRO TEOR"}   # afetação de repetitivos não tem os dois


def contar_no_pdf(caminho):
    with pdfplumber.open(caminho) as pdf:
        linhas = [l.strip() for p in pdf.pages for l in (p.extract_text() or "").splitlines()]
    return {
        "PROCESSO": sum(1 for l in linhas if l.startswith("PROCESSO ")),
        "DESTAQUE": sum(1 for l in linhas if l == "DESTAQUE"),
        "TEOR": sum(1 for l in linhas if l == "INFORMAÇÕES DO INTEIRO TEOR"),
    }


def main(pasta, excel, edicoes=()):
    df = pd.read_excel(excel).fillna("")
    if "SECAO" not in df.columns:
        df["SECAO"] = ""
    if "OBS" not in df.columns:
        df["OBS"] = ""
    julgados = df.drop_duplicates(subset=CHAVE)

    esperado = {}
    pdfs = sorted(f for f in os.listdir(pasta) if f.lower().endswith(".pdf"))
    if edicoes:
        pdfs = [f for f in pdfs if f[3:-4] in edicoes]
        df = df[df["ARQUIVO"].isin(pdfs)]
        julgados = df.drop_duplicates(subset=CHAVE)
    for i, f in enumerate(pdfs, 1):
        esperado[f] = contar_no_pdf(os.path.join(pasta, f))
        if i % 50 == 0:
            print(f"  ... {i}/{len(pdfs)} PDFs lidos")

    linhas = []
    for f in pdfs:
        j = julgados[julgados["ARQUIVO"] == f]
        obtido = {"PROCESSO": len(j), "DESTAQUE": int((j["DESTAQUE"] != "").sum()), "TEOR": int((j["INTEIRO_TEOR"] != "").sum())}
        for campo in ("PROCESSO", "DESTAQUE", "TEOR"):
            if esperado[f][campo] != obtido[campo]:
                linhas.append({"ARQUIVO": f, "CAMPO": campo, "NO_PDF": esperado[f][campo], "NO_EXCEL": obtido[campo]})
    divergencias = pd.DataFrame(linhas, columns=["ARQUIVO", "CAMPO", "NO_PDF", "NO_EXCEL"])

    print(f"\nPDFs na pasta: {len(pdfs)} | edições no Excel: {df['ARQUIVO'].nunique()} | julgados no Excel: {len(julgados)} | linhas (um por ramo): {len(df)}")
    sem_excel = sorted(set(pdfs) - set(df["ARQUIVO"]))
    sem_pdf = sorted(set(df["ARQUIVO"]) - set(pdfs))
    if sem_excel: print(f"\nPDFs que NÃO aparecem no Excel ({len(sem_excel)}): {sem_excel[:15]}{' ...' if len(sem_excel) > 15 else ''}")
    if sem_pdf: print(f"\nEdições no Excel sem PDF na pasta ({len(sem_pdf)}): {sem_pdf[:15]}")

    print("\n=== 1) Contagem PDF x Excel ===")
    if divergencias.empty:
        print("Nenhuma divergência: todos os blocos, DESTAQUEs e INTEIROS TEORES batem.")
    else:
        print(f"{divergencias['ARQUIVO'].nunique()} edições com divergência (veja relatorio_validacao.csv). Primeiras:")
        print(divergencias.head(25).to_string(index=False))
    divergencias.to_csv("relatorio_validacao.csv", index=False)

    print("\n=== 2) Lixo de página dentro dos campos ===")
    achou = False
    for campo in ("PROCESSO", "TEMA", "DESTAQUE", "INTEIRO_TEOR", "RAMO_DO_DIREITO"):
        for padrao in LIXO:
            if campo == "INTEIRO_TEOR" and padrao in (r"INFORMAÇÕES DO INTEIRO TEOR",):
                continue
            ocorr = julgados[julgados[campo].astype(str).str.contains(padrao, regex=True)]
            if len(ocorr):
                achou = True
                print(f"{campo} contém /{padrao}/ em {len(ocorr)} julgados, ex.: {sorted(ocorr['ARQUIVO'].unique())[:5]}")
    if not achou: print("Nenhum.")

    print("\n=== 3) Campos vazios ===")
    for campo in ("PROCESSO", "RAMO_DO_DIREITO", "TEMA"):
        vazios = df[df[campo].astype(str).str.strip() == ""]
        print(f"{campo} vazio: {len(vazios)}" + (f" | ex.: {sorted(vazios['ARQUIVO'].unique())[:5]}" if len(vazios) else ""))

    print("\n=== 4) Avisos da coluna OBS (além do esperado nas afetações) ===")
    obs = julgados[julgados["OBS"] != ""].groupby("OBS")["ARQUIVO"].agg(["count", lambda s: sorted(s.unique())[:3]])
    obs.columns = ["julgados", "exemplos"]
    inesperadas = obs[~obs.index.isin(OBS_ESPERADA)]
    print(inesperadas.to_string() if len(inesperadas) else "Nenhum aviso inesperado.")
    print(f"(esperados, 'sem DESTAQUE; sem INTEIRO TEOR': {int(obs['julgados'].get('sem DESTAQUE; sem INTEIRO TEOR', 0))} julgados)")

    print("\n=== 5a) Seções do informativo (confira se alguma parece pedaço de frase) ===")
    print(julgados["SECAO"].value_counts().to_string())
    print("\n=== 5) Ramos do direito (confira se algum valor parece pedaço de frase) ===")
    print(df["RAMO_DO_DIREITO"].value_counts().to_string())
    return 0 if divergencias.empty else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2], tuple(sys.argv[3:])))
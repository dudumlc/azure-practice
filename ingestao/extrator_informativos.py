"""
Extrator do Informativo de Jurisprudência do STJ (PDF -> tabela).

Diferenças em relação ao regex único anterior:
  - lê o PDF linha a linha, em vez de um regex gigante com re.DOTALL;
  - remove rodapé ("processo.stj.jus.br/.../ 3/29") e cabeçalho de cada página
    antes de extrair, para que não caiam dentro de TEMA, DESTAQUE ou INTEIRO TEOR;
  - cada bloco (PROCESSO ... ) termina no próximo PROCESSO, então blocos sem
    DESTAQUE (afetação de repetitivos) não são engolidos pelo bloco seguinte;
  - DESTAQUE e INTEIRO TEOR são lidos dentro do próprio bloco, sem pareamento por índice;
  - o ramo do direito é separado por vírgula e limpo com strip().
"""
import io
import os
import re

import pandas as pd
import pdfplumber

# ---------------------------------------------------------------- ruído de página
RE_RODAPE = re.compile(r"^processo\.stj\.jus\.br/jurisprudencia/externo/informativo/\s*\d+\s*/\s*\d+$")
RE_CABECALHO = re.compile(
    r"^(Informativo|de Jurisprudência|Informativo de Jurisprudência n\..*"
    r"|Número \d+ Brasília,.*|Este periódico destaca.*)$"
)
LINHAS_TOPO = 6  # cabeçalho só é removido nas primeiras linhas de cada página

# ---------------------------------------------------------------- estrutura do bloco
# Linhas que encerram o conteúdo útil do bloco (o resto é "informações adicionais").
FIM_DE_BLOCO = {
    "INFORMAÇÕES ADICIONAIS", "LEGISLAÇÃO", "SÚMULAS", "PRECEDENTES QUALIFICADOS",
    "SAIBA MAIS", "ÁUDIO DO TEXTO", "VÍDEO DO JULGAMENTO ÁUDIO DO TEXTO",
}
# Cabeçalhos de seção que ficam colados no fim do bloco anterior.
RE_SECAO = re.compile(
    r"^(CORTE ESPECIAL|(PRIMEIRA|SEGUNDA|TERCEIRA|QUARTA|QUINTA|SEXTA|SÉTIMA|OITAVA) (TURMA|SEÇÃO)"
    r"|RECURSOS REPETITIVOS.*)$"
)
ORDEM = {"PROCESSO": 0, "RAMO": 1, "TEMA": 2, "DESTAQUE": 3, "TEOR": 4}
RE_CAIXA_ALTA = re.compile(r"^[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ ,\-/]{5,}$")

LIMITE_CELULA_EXCEL = 32000  # o Excel aceita no máximo 32.767 caracteres por célula

COLUNAS = ["PROCESSO", "RAMO_DO_DIREITO", "TEMA", "DESTAQUE", "INTEIRO_TEOR", "ARQUIVO", "LINK", "OBS"]


def _normalizar(linhas):
    return " ".join(" ".join(linhas).split())


def _linhas_limpas(paginas):
    """Texto de cada página -> lista única de linhas, sem rodapé nem cabeçalho."""
    saida = []
    for texto in paginas:
        linhas = [l.strip() for l in (texto or "").splitlines()]
        topo = [i for i, l in enumerate(linhas) if l][:LINHAS_TOPO]
        for i, l in enumerate(linhas):
            if not l or RE_RODAPE.match(l):
                continue
            if i in topo and RE_CABECALHO.match(l):
                continue
            saida.append(l)
    return saida


def _inicio_valido(linhas, i):
    """'PROCESSO ...' só abre um bloco se um 'RAMO DO DIREITO' vier logo depois."""
    for l in linhas[i + 1:i + 40]:
        if l.startswith("RAMO DO DIREITO"):
            return True
        if l.startswith("PROCESSO "):
            return False
    return False


def _ler_bloco(corpo):
    campos = {k: [] for k in ORDEM}
    campos["PROCESSO"].append(corpo[0][len("PROCESSO"):])
    atual = "PROCESSO"

    def mudar(novo):
        return ORDEM[novo] > ORDEM[atual]  # os marcadores só valem na ordem do informativo

    for l in corpo[1:]:
        if l in FIM_DE_BLOCO:
            break
        if l.startswith("RAMO DO DIREITO") and mudar("RAMO"):
            atual = "RAMO"
            campos[atual].append(l[len("RAMO DO DIREITO"):])
        elif (l == "TEMA" or l.startswith("TEMA ")) and mudar("TEMA"):
            atual = "TEMA"
            campos[atual].append(l[len("TEMA"):])
        elif l == "DESTAQUE" and mudar("DESTAQUE"):
            atual = "DESTAQUE"
        elif l == "INFORMAÇÕES DO INTEIRO TEOR" and mudar("TEOR"):
            atual = "TEOR"
        else:
            campos[atual].append(l)

    obs = []
    for nome, rotulo in (("RAMO", "RAMO"), ("TEMA", "TEMA"), ("DESTAQUE", "DESTAQUE"), ("TEOR", "INTEIRO TEOR")):
        if not campos[nome]:
            obs.append(f"sem {rotulo}")
    for nome in ("TEMA", "DESTAQUE", "TEOR"):
        if campos[nome] and RE_CAIXA_ALTA.match(campos[nome][-1]):
            obs.append(f"{nome}: última linha em caixa alta (cabeçalho?)")

    return {
        "PROCESSO": _normalizar(campos["PROCESSO"]),
        "RAMO_DO_DIREITO": _normalizar(campos["RAMO"]),
        "TEMA": _normalizar(campos["TEMA"]),
        "DESTAQUE": _normalizar(campos["DESTAQUE"]),
        "INTEIRO_TEOR": _normalizar(campos["TEOR"]),
        "OBS": "; ".join(obs),
    }


def extrair_blocos(paginas):
    """Lista de textos de página -> lista de dicts, um por julgado."""
    linhas = _linhas_limpas(paginas)
    inicios = [i for i, l in enumerate(linhas) if l.startswith("PROCESSO ") and _inicio_valido(linhas, i)]
    blocos = []
    for k, ini in enumerate(inicios):
        fim = inicios[k + 1] if k + 1 < len(inicios) else len(linhas)
        corpo = linhas[ini:fim]
        while len(corpo) > 1 and RE_SECAO.match(corpo[-1]):
            corpo.pop()
        blocos.append(_ler_bloco(corpo))
    return blocos


def ler_pdf(origem):
    """origem: caminho do arquivo (carga local) ou os bytes do PDF (produção, baixado em memória)."""
    if isinstance(origem, (bytes, bytearray)):
        origem = io.BytesIO(origem)
    with pdfplumber.open(origem) as pdf:
        paginas = [p.extract_text() for p in pdf.pages]
    return extrair_blocos(paginas)


def extracao_infos(pasta, arquivo_resultado):
    if os.path.exists(arquivo_resultado):
        df_lido = pd.read_excel(arquivo_resultado)
    else:
        df_lido = pd.DataFrame(columns=COLUNAS)
    arquivos_lidos = set(df_lido["ARQUIVO"].unique())
    arquivos_totais = sorted(f for f in os.listdir(pasta) if f.lower().endswith(".pdf"))

    resultados = []
    for file in arquivos_totais:
        if file in arquivos_lidos:
            continue
        print(f"Lendo arquivo: {file}")
        blocos = ler_pdf(os.path.join(pasta, file))
        for dados in blocos:
            dados["ARQUIVO"] = file
            resultados.append(dados)
        print(f"  {len(blocos)} blocos extraídos")

    df_novo = pd.DataFrame(resultados)
    if df_novo.empty:
        return "Todos os arquivos já haviam sido lidos."

    # link do PDF
    df_novo["LINK"] = ("https://processo.stj.jus.br/SCON/GetPDFINFJ?edicao="
                       + df_novo["ARQUIVO"].str.replace("Inf", "").str.replace(".pdf", ""))

    # uma linha por ramo do direito (separa só por vírgula: "DIREITO CIVIL E PROCESSUAL CIVIL" é um ramo só)
    df_novo["RAMO_DO_DIREITO"] = df_novo["RAMO_DO_DIREITO"].str.split(",")
    df_novo = df_novo.explode("RAMO_DO_DIREITO").reset_index(drop=True)
    df_novo["RAMO_DO_DIREITO"] = df_novo["RAMO_DO_DIREITO"].str.strip()

    # o Excel não aceita mais de 32.767 caracteres por célula
    longas = df_novo["INTEIRO_TEOR"].str.len() > LIMITE_CELULA_EXCEL
    df_novo.loc[longas, "OBS"] = (
        df_novo.loc[longas, "OBS"] + "; INTEIRO TEOR truncado para caber no Excel").str.strip("; ")
    df_novo["INTEIRO_TEOR"] = df_novo["INTEIRO_TEOR"].str.slice(0, LIMITE_CELULA_EXCEL)

    df_final = pd.concat([df_lido, df_novo], ignore_index=True)[COLUNAS]
    os.makedirs(os.path.dirname(arquivo_resultado) or ".", exist_ok=True)
    df_final.to_excel(arquivo_resultado, index=False)
    return f"Arquivo {os.path.basename(arquivo_resultado)} atualizado com as informações dos novos PDFs."
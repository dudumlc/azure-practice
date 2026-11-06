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
# Títulos de seção do informativo (aparecem uma vez, antes do primeiro processo da seção).
# Lista conferida nos 269 PDFs; um título quebrado em duas linhas traz a 2ª linha em CONTINUACAO_TITULO.
RE_SECAO = re.compile(
    r"^((PRIMEIRA|SEGUNDA|TERCEIRA|QUARTA|QUINTA|SEXTA) TURMA"
    r"|(PRIMEIRA|SEGUNDA|TERCEIRA) SEÇÃO"
    r"|CORTE ESPECIAL( - JULGAMENTO NÃO CONCLUÍDO)?"
    r"|RECURSOS REPETITIVOS( - (AFETAÇÃO|CANCELAMENTOS? DE AFETAÇÃO))?"
    r"|INCIDENTE DE ASSUNÇÃO DE COMPETÊNCIA( - AFETAÇÃO)?"
    r"|PROPOSTA DE (REVISÃO|RELEVÂNCIA) .*)$"
)
RE_CONTINUACAO_TITULO = re.compile(r"^(REPETITIVO|INFRACONSTITUCIONAL)$")


def _secao_canonica(titulo):
    titulo = titulo.replace("CANCELAMENTOS DE", "CANCELAMENTO DE")
    if titulo.startswith("PROPOSTA DE REVISÃO"):
        return "PROPOSTA DE REVISÃO DE ENTENDIMENTO FIRMADO EM TEMA REPETITIVO"
    if titulo.startswith("PROPOSTA DE RELEVÂNCIA"):
        return "PROPOSTA DE RELEVÂNCIA DA QUESTÃO DE DIREITO FEDERAL INFRACONSTITUCIONAL"
    return titulo


ORDEM = {"PROCESSO": 0, "RAMO": 1, "TEMA": 2, "DESTAQUE": 3, "TEOR": 4}
RE_CAIXA_ALTA = re.compile(r"^[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ ,\-/]{5,}$")

LIMITE_CELULA_EXCEL = 32000  # o Excel aceita no máximo 32.767 caracteres por célula

COLUNAS = ["PROCESSO", "RAMO_DO_DIREITO", "SECAO", "TEMA", "DESTAQUE", "INTEIRO_TEOR", "ARQUIVO", "LINK", "OBS"]


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


MARCADORES_DE_CAMPO = ("RAMO DO DIREITO", "TEMA", "DESTAQUE", "INFORMAÇÕES DO INTEIRO TEOR")


def _inicio_valido(linhas, i):
    """'PROCESSO ...' só abre um bloco se for mesmo uma linha de processo e vier seguida de um campo.

    O primeiro marcador depois dele pode ser RAMO DO DIREITO, TEMA, DESTAQUE ou INFORMAÇÕES DO INTEIRO TEOR
    (há blocos sem RAMO, sem TEMA etc.), mas não outro PROCESSO nem o fim do bloco. O texto do processo
    precisa trazer "Rel." ou "julgado em", o que descarta linhas de texto que apenas começam com PROCESSO.
    Sem limite de distância: um bloco com muitos recursos relacionados tem um PROCESSO bem longo.
    """
    cabecalho = [linhas[i]]
    for l in linhas[i + 1:]:
        if l.startswith("PROCESSO ") or l in FIM_DE_BLOCO:
            return False
        if l.startswith("RAMO DO DIREITO") or l == "TEMA" or l.startswith("TEMA ") or l == "DESTAQUE" \
                or l == "INFORMAÇÕES DO INTEIRO TEOR":
            texto = " ".join(cabecalho)
            return "Rel." in texto or "julgado em" in texto or SEM_PROCESSO in texto
        cabecalho.append(l)
    return False


def _ler_bloco(corpo):
    campos = {k: [] for k in ORDEM}
    campos["PROCESSO"].append(corpo[0][len("PROCESSO"):])
    atual = "PROCESSO"
    fora_de_ordem = False

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
            if l.startswith("RAMO DO DIREITO") or l in ("DESTAQUE", "INFORMAÇÕES DO INTEIRO TEOR"):
                fora_de_ordem = True   # marcador no lugar errado: o bloco pode estar colado em outro
            campos[atual].append(l)

    obs = []
    if fora_de_ordem:
        obs.append("marcador fora de ordem (bloco possivelmente colado em outro)")
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


SEM_PROCESSO = "processo não informado no informativo"
RE_PROCESSO_NO_TEMA = re.compile(r"\b((?:E?A?REsp|RHC|HC|CC|IAC|Pet|MS)s?)\s*(?:n\.\s*)?(\d[\d.]*[-/][A-Z]{2})")


def _inserir_processo_ausente(linhas):
    """Em alguns julgados o PDF do STJ traz só RAMO, TEMA... sem a linha PROCESSO (ex.: afetações das edições 0800,
    0807, 0810, 0811, 0821; julgados soltos nas edições 0818 e 0834). Cria-se um cabeçalho sintético quando:
      - um segundo RAMO DO DIREITO aparece depois de um TEMA, antes de qualquer PROCESSO novo; ou
      - um RAMO aparece depois do fim de um bloco (LEGISLAÇÃO etc.) sem PROCESSO novo.
    Depois de SAIBA MAIS não se cria nada: ali o STJ cita julgados de informativos anteriores, que não são desta edição."""
    saida, viu_tema, fim_visto, saiba_mais = [], False, False, False
    for i, l in enumerate(linhas):
        if l.startswith("PROCESSO "):
            viu_tema = fim_visto = saiba_mais = False
        elif l in FIM_DE_BLOCO:
            viu_tema, fim_visto = False, True
            if l == "SAIBA MAIS":
                saiba_mais = True
        elif l == "TEMA" or l.startswith("TEMA "):
            viu_tema = True
        elif l.startswith("RAMO DO DIREITO") and (viu_tema or (fim_visto and not saiba_mais)):
            trecho = " ".join(linhas[i + 1:i + 9])
            achado = RE_PROCESSO_NO_TEMA.search(trecho)
            tema_n = re.search(r"\bTema\s+(?:n\.\s*)?(\d+)", trecho)
            if achado:
                citado = f"; TEMA cita {achado.group(1)} {achado.group(2)}"
            elif tema_n:
                citado = f"; Tema {tema_n.group(1)}"
            else:
                citado = ""
            saida.append(f"PROCESSO ({SEM_PROCESSO}{citado})")
            viu_tema = fim_visto = False
        saida.append(l)
    return saida


def extrair_blocos(paginas):
    """Lista de textos de página -> lista de dicts, um por julgado, cada um com a SEÇÃO do informativo."""
    linhas = _inserir_processo_ausente(_linhas_limpas(paginas))
    titulos = [(i, _secao_canonica(l)) for i, l in enumerate(linhas) if RE_SECAO.match(l)]
    inicios = [i for i, l in enumerate(linhas) if l.startswith("PROCESSO ") and _inicio_valido(linhas, i)]
    blocos = []
    for k, ini in enumerate(inicios):
        fim = inicios[k + 1] if k + 1 < len(inicios) else len(linhas)
        corpo = linhas[ini:fim]
        while len(corpo) > 1 and (RE_SECAO.match(corpo[-1]) or RE_CONTINUACAO_TITULO.match(corpo[-1])):
            corpo.pop()
        bloco = _ler_bloco(corpo)
        if SEM_PROCESSO in corpo[0]:
            bloco["OBS"] = "; ".join(x for x in (bloco["OBS"], "PROCESSO ausente no PDF") if x)
        anteriores = [t for pos, t in titulos if pos < ini]   # o título vale até o próximo título
        bloco["SECAO"] = anteriores[-1] if anteriores else ""
        if not bloco["SECAO"]:
            bloco["OBS"] = "; ".join(x for x in (bloco["OBS"], "sem SEÇÃO") if x)
        blocos.append(bloco)
    return blocos


def ler_pdf_detalhado(origem):
    """-> (blocos, quantas linhas "PROCESSO ..." o PDF tem). A contagem serve de conferência independente do extrator."""
    if isinstance(origem, (bytes, bytearray)):
        origem = io.BytesIO(origem)
    with pdfplumber.open(origem) as pdf:
        paginas = [p.extract_text() for p in pdf.pages]
    n_processo = sum(1 for p in paginas for l in (p or "").splitlines() if l.strip().startswith("PROCESSO "))
    return extrair_blocos(paginas), n_processo


def ler_pdf(origem):
    """origem: caminho do arquivo (carga local) ou os bytes do PDF (produção, baixado em memória)."""
    return ler_pdf_detalhado(origem)[0]


def extracao_infos(pasta, arquivo_resultado):
    import pandas as pd   # só a carga em Excel usa; a Function de produção não precisa
    if os.path.exists(arquivo_resultado):
        df_lido = pd.read_excel(arquivo_resultado)
    else:
        df_lido = pd.DataFrame(columns=COLUNAS)
    arquivos_lidos = set(df_lido["ARQUIVO"].unique())
    arquivos_totais = sorted(f for f in os.listdir(pasta) if f.lower().endswith(".pdf"))

    if not arquivos_totais:
        return f"Nenhum PDF encontrado em {pasta}"

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
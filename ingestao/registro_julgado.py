"""
Monta os documentos do Cosmos DB a partir dos blocos extraídos dos informativos.

Sem dependência de pandas nem de Azure: é usado tanto pela carga local (Excel) quanto,
mais adiante, pela Function de ingestão (PDF baixado em memória).

Um "bloco" é o dict que o extrator devolve:
    PROCESSO, RAMO_DO_DIREITO (texto, ramos separados por vírgula), TEMA, DESTAQUE, INTEIRO_TEOR, OBS
"""
import hashlib
import re
import unicodedata
from datetime import datetime, timezone

RE_PRINCIPAL = re.compile(r"^(.*?),\s*Rel\.")
URL_EDICAO = "https://processo.stj.jus.br/SCON/GetPDFINFJ?edicao={edicao}"
META_ID = "meta"
META_PARTICAO = "_meta"


def normalizar(texto):
    """minúsculas e sem acento, para a busca por termo."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    return sem_acento


def processo_principal(processo):
    m = RE_PRINCIPAL.match(processo)
    return m.group(1).strip() if m else processo[:80].strip()


def edicao_do_arquivo(arquivo):
    """'Inf0814.pdf' -> '0814' ; 'Inf0025E.pdf' -> '0025E'"""
    return arquivo[3:-4]


def montar_docs(edicao, blocos):
    """Blocos de UMA edição -> lista de documentos. Ids estáveis (não dependem do TEMA)."""
    ocorrencias = {}
    docs = []
    for b in blocos:
        proc = b["PROCESSO"].strip()
        principal = processo_principal(proc)
        n = ocorrencias.get(principal, 0) + 1
        ocorrencias[principal] = n
        ramos = []
        for r in str(b["RAMO_DO_DIREITO"]).split(","):
            r = r.strip()
            if r and r not in ramos:
                ramos.append(r)
        tema, destaque = b["TEMA"].strip(), b["DESTAQUE"].strip()
        docs.append({
            "id": hashlib.sha1(f"{edicao}|{principal}|{n}".encode()).hexdigest()[:16],
            "edicao": edicao,
            "processo_principal": principal,
            "processo": proc,
            "ramos": ramos,
            "tema": tema,
            "destaque": destaque,
            "inteiro_teor": b["INTEIRO_TEOR"].strip(),
            "tema_busca": normalizar(tema),
            "destaque_busca": normalizar(destaque),
            "link": URL_EDICAO.format(edicao=edicao),
            "obs": [o for o in str(b.get("OBS", "")).split("; ") if o],
            "resumo_ia": None,
        })
    return docs


def montar_meta(edicoes, total_julgados, versao_parser, a_revisar=()):
    """Documento de controle (Tela 3 e botão 'verificar atualização')."""
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    numeradas = [e for e in edicoes if e.isdigit()]
    return {
        "id": META_ID,
        "edicao": META_PARTICAO,
        "ultima_edicao": max(numeradas) if numeradas else None,
        "total_edicoes": len(edicoes),
        "total_julgados": total_julgados,
        "atualizado_em": agora,
        "ultima_verificacao": agora,
        "fonte": "STJ - Informativo de Jurisprudência (processo.stj.jus.br)",
        "modelos": None,
        "versao_parser": versao_parser,
        "edicoes_a_revisar": sorted(a_revisar),
    }
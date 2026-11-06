"""
Monta os documentos do Cosmos DB a partir dos blocos extraídos dos informativos.

Sem dependência de pandas nem de Azure: é usado tanto pela carga local (Excel) quanto,
mais adiante, pela Function de ingestão (PDF baixado em memória).

Avisos do informativo (nota excluída, destaque alterado, errata) são ignorados: ver eh_aviso().
Um "bloco" é o dict que o extrator devolve:
    PROCESSO, RAMO_DO_DIREITO (texto, ramos separados por vírgula), SECAO, TEMA, DESTAQUE, INTEIRO_TEOR, OBS
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


def ramos_do_bloco(bloco):
    ramos = []
    for r in str(bloco["RAMO_DO_DIREITO"]).split(","):
        r = r.strip()
        if r and r not in ramos:
            ramos.append(r)
    return ramos


def eh_aviso(bloco):
    """Aviso do próprio informativo ("nota excluída", "destaque alterado", errata), não um julgado.

    Esses blocos não têm RAMO DO DIREITO e também lhes falta o TEMA ou o DESTAQUE. Já um julgado de
    verdade sem ramo (existem alguns) tem TEMA e DESTAQUE e entra na base.
    """
    return not ramos_do_bloco(bloco) and not (bloco["TEMA"].strip() and bloco["DESTAQUE"].strip())


def sem_repetidos(blocos):
    """O STJ às vezes publica o mesmo julgado mais de uma vez na mesma edição (ex.: 0720). Mantém um só.

    Mesma chave da carga em Excel (ingestao_spot_excel.py), para os dois caminhos gerarem os mesmos ids.
    """
    vistos, unicos = set(), []
    for b in blocos:
        chave = tuple(str(b.get(c, "")).strip() for c in
                      ("PROCESSO", "SECAO", "TEMA", "DESTAQUE", "INTEIRO_TEOR", "OBS"))
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(b)
    return unicos


def montar_docs(edicao, blocos):
    """Blocos de UMA edição -> lista de documentos. Ids estáveis (não dependem do TEMA)."""
    ocorrencias = {}
    docs = []
    for b in blocos:
        proc = b["PROCESSO"].strip()
        if eh_aviso(b):
            continue
        ramos = ramos_do_bloco(b)
        tema, destaque = b["TEMA"].strip(), b["DESTAQUE"].strip()
        principal = processo_principal(proc)
        n = ocorrencias.get(principal, 0) + 1
        ocorrencias[principal] = n
        docs.append({
            "id": hashlib.sha1(f"{edicao}|{principal}|{n}".encode()).hexdigest()[:16],
            "edicao": edicao,
            "processo_principal": principal,
            "processo": proc,
            "secao": str(b.get("SECAO", "")).strip(),
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
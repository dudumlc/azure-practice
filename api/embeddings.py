"""
Vetores (embeddings) dos julgados com o Gemini.

Usado pela geração em lote (ingestao/gerar_embeddings.py) e, depois, pela busca por descrição e pela
atualização semanal. Fica em api/ porque só essa pasta vai para o Azure.
"""
import math
import time

import requests

MODELO = "gemini-embedding-001"
DIMENSOES = 768                 # o modelo gera 3072; 768 ocupa 4x menos espaço e perde pouco
MAX_CHARS = 6000                # o modelo aceita ~2048 tokens por texto
LOTE = 50                       # textos por chamada
PAUSA_ENTRE_LOTES = 32          # segundos: 50 textos a cada ~32 s = ~95 por minuto, abaixo do limite gratuito (100/min)
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:batchEmbedContents"


class CotaEsgotada(Exception):
    """O Gemini recusou por limite de uso (por minuto ou por dia)."""


class ErroGemini(Exception):
    """Resposta de erro do Gemini que não adianta repetir (chave inválida, pedido malformado...)."""


def texto_do_julgado(doc):
    return f"{doc.get('tema', '')}\n{doc.get('destaque', '')}".strip()[:MAX_CHARS]


def normalizar_vetor(v):
    """Com menos de 3072 dimensões o Gemini não devolve o vetor normalizado: normaliza aqui."""
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embutir(textos, tipo, chave_api, sessao=requests, esperar=time.sleep, progresso=print,
            ao_terminar_lote=None, lote=LOTE, pausa=PAUSA_ENTRE_LOTES, tentativas=7, timeout=120):
    """Vetoriza `textos` (tipo: RETRIEVAL_DOCUMENT para julgados, RETRIEVAL_QUERY para a busca).

    ao_terminar_lote(textos_do_lote, vetores_do_lote) é chamado a cada lote pronto (para salvar o progresso).
    `tentativas` e `timeout` menores servem a busca do site, que não pode esperar minutos pelo Gemini."""
    vetores = []
    for ini in range(0, len(textos), lote):
        parte = textos[ini:ini + lote]
        corpo = {"requests": [{"model": f"models/{MODELO}", "content": {"parts": [{"text": t}]},
                               "taskType": tipo, "outputDimensionality": DIMENSOES} for t in parte]}
        for tentativa in range(1, tentativas + 1):
            r = sessao.post(URL, json=corpo, headers={"x-goog-api-key": chave_api}, timeout=timeout)
            if r.status_code == 200:
                novos = [normalizar_vetor(e["values"]) for e in r.json()["embeddings"]]
                vetores += novos
                if ao_terminar_lote:
                    ao_terminar_lote(parte, novos)
                break
            if r.status_code == 429 and ("PerDay" in r.text or "per day" in r.text.lower()):
                raise CotaEsgotada("limite diário do plano gratuito atingido")
            if r.status_code in (429, 500, 503) and tentativa < tentativas:
                espera = min(15 * tentativa, 70)
                progresso(f"  Gemini respondeu {r.status_code} (limite de uso); esperando {espera}s…")
                esperar(espera)
                continue
            if r.status_code == 429:
                raise CotaEsgotada("o Gemini continua recusando por limite de uso")
            raise ErroGemini(f"Erro do Gemini {r.status_code}: {r.text[:400]}")
        progresso(f"  vetorizados {min(ini + lote, len(textos))}/{len(textos)}")
        if ini + lote < len(textos):
            esperar(pausa)                      # ritmo constante, para não bater no limite por minuto
    return vetores

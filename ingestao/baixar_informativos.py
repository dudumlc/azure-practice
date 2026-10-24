"""
Baixa os PDFs do Informativo de Jurisprudência do STJ.

Dois modos:
  - Automático (produção): começa na edição seguinte à maior que já está na pasta e segue até
    não achar mais edição nova (3 inexistentes seguidas).
        python baixar_informativos.py
        python baixar_informativos.py --inicio 856          # automático, mas começando na 856
  - Intervalo fixo (cargas pontuais):
        python baixar_informativos.py --inicio 856 --fim 906

Os arquivos saem como Inf0856.pdf, Inf0857.pdf... (o mesmo padrão que o extrator usa).
Quem já existe na pasta é pulado, então pode rodar de novo à vontade.
Edições extraordinárias não têm numeração contínua: use --extras 0026E,0027E.
"""
import argparse
import itertools
import os
import sys
import time

import requests

URL = "https://processo.stj.jus.br/SCON/GetPDFINFJ?edicao={edicao}"
USER_AGENT = "buscador-jurisprudencia/0.1 (uso pessoal)"
PAUSA = 2.0            # segundos entre uma edição e outra
TENTATIVAS = 3         # por edição, só para erro de rede/bloqueio
MAX_BLOQUEIOS = 3      # edições seguidas bloqueadas -> para tudo
MAX_AUSENTES = 3       # no modo automático: edições inexistentes seguidas = chegou ao fim


def baixar_pdf(sessao, url):
    """Devolve ("ok", bytes) | ("sem_pdf", motivo) | ("bloqueio", motivo)."""
    motivo = ""
    for tentativa in range(TENTATIVAS):
        try:
            r = sessao.get(url, timeout=60)
        except requests.RequestException as e:
            motivo = f"erro de rede: {e.__class__.__name__}"
        else:
            if r.status_code == 200:
                if r.content.startswith(b"%PDF"):
                    return "ok", r.content
                return "sem_pdf", "a resposta não é um PDF"
            if r.status_code in (404, 410):
                return "sem_pdf", f"HTTP {r.status_code}"
            if r.status_code in (403, 429) or r.status_code >= 500:
                motivo = f"HTTP {r.status_code}"
                espera = r.headers.get("Retry-After", "")
                if espera.isdigit():
                    time.sleep(min(int(espera), 120))
                    continue
            else:
                return "sem_pdf", f"HTTP {r.status_code}"
        time.sleep(5 * 2 ** tentativa)
    return "bloqueio", motivo


def obter(sessao, args, ed):
    """Garante o PDF da edição na pasta. Devolve ("existe"|"ok"|"sem_pdf"|"bloqueio", detalhe)."""
    destino = os.path.join(args.pasta, f"Inf{ed}.pdf")
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return "existe", ""
    status, dado = baixar_pdf(sessao, args.url.format(edicao=ed))
    if status != "ok":
        return status, dado
    with open(destino + ".part", "wb") as fh:
        fh.write(dado)
    os.replace(destino + ".part", destino)   # só aparece na pasta se baixou inteiro
    return "ok", f"{len(dado) / 1024:.0f} KB"


def maior_edicao_na_pasta(pasta):
    """Maior número entre os Inf####.pdf da pasta (ignora extraordinárias como Inf0025E.pdf)."""
    nums = [int(f[3:7]) for f in os.listdir(pasta)
            if len(f) == 11 and f.startswith("Inf") and f.endswith(".pdf") and f[3:7].isdigit()]
    return max(nums) if nums else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inicio", type=int, help="primeira edição (padrão: a seguinte à maior que já está na pasta)")
    ap.add_argument("--fim", type=int, help="última edição; sem ela, segue até não haver mais edição nova")
    ap.add_argument("--pasta", default="pdfs")
    ap.add_argument("--extras", default="", help="edições extraordinárias separadas por vírgula, ex.: 0026E")
    ap.add_argument("--url", default=URL, help="modelo da URL, com {edicao}")
    args = ap.parse_args()

    os.makedirs(args.pasta, exist_ok=True)
    inicio = args.inicio
    if inicio is None:
        ultima = maior_edicao_na_pasta(args.pasta)
        if ultima is None:
            print("A pasta está vazia. Informe a primeira edição com --inicio (ex.: --inicio 856).")
            return 2
        inicio = ultima + 1
    automatico = args.fim is None
    numeros = itertools.count(inicio) if automatico else range(inicio, args.fim + 1)
    print(f"Modo {'automático' if automatico else 'intervalo fixo'}, a partir da edição {inicio:04d}.")

    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    baixados, existentes, falhas = [], [], []
    ausentes = []              # inexistentes seguidos: ainda não sabemos se é lacuna ou fim da fila
    bloqueios_seguidos = 0
    fim_da_fila = False
    for n in numeros:
        ed = f"{n:04d}"
        status, detalhe = obter(sessao, args, ed)

        if status in ("ok", "existe"):
            falhas += ausentes                 # inexistentes antes de uma edição que existe = lacuna
            ausentes, bloqueios_seguidos = [], 0
            (baixados if status == "ok" else existentes).append(ed)
            if status == "ok":
                print(f"{ed}: baixado ({detalhe})")
        elif status == "sem_pdf":
            ausentes.append((ed, detalhe))
            bloqueios_seguidos = 0
            print(f"{ed}: sem PDF ({detalhe})")
            if automatico and len(ausentes) >= MAX_AUSENTES:
                fim_da_fila = True
                break
        else:  # bloqueio
            falhas.append((ed, detalhe))
            bloqueios_seguidos += 1
            print(f"{ed}: FALHOU ({detalhe})")
            if bloqueios_seguidos >= MAX_BLOQUEIOS:
                print(f"\n{MAX_BLOQUEIOS} edições seguidas bloqueadas. Parei para não insistir no site.")
                print("Espere um pouco e rode de novo: o que já foi baixado é pulado.")
                break
        if status != "existe":
            time.sleep(PAUSA)

    if not fim_da_fila:
        falhas += ausentes
    for ed in [e.strip() for e in args.extras.split(",") if e.strip()]:
        status, detalhe = obter(sessao, args, ed)
        if status == "ok":
            baixados.append(ed)
            print(f"{ed}: baixado ({detalhe})")
        elif status == "existe":
            existentes.append(ed)
        else:
            falhas.append((ed, detalhe))
            print(f"{ed}: FALHOU ({detalhe})")
        if status != "existe":
            time.sleep(PAUSA)

    if falhas:
        with open("falhas_download.txt", "w", encoding="utf-8") as fh:
            for ed, motivo in falhas:
                fh.write(f"{ed}\t{motivo}\n")
    elif os.path.exists("falhas_download.txt"):
        os.remove("falhas_download.txt")      # não deixa a lista de uma rodada antiga parecendo atual
    if fim_da_fila:
        print(f"\nSem edição nova a partir da {ausentes[0][0]}.")
    print(f"Baixados: {len(baixados)} | já existiam: {len(existentes)} | falhas: {len(falhas)}"
          + (" (veja falhas_download.txt)" if falhas else ""))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
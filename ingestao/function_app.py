"""
Function App de ingestão: reage a uma mensagem na fila "fila-atualizacao" e atualiza a base.

Não tem rota HTTP: quem enfileira é a API do Static Web App, depois de checar a espera entre verificações.
Variáveis de ambiente: COSMOS_CONNECTION_STRING e AzureWebJobsStorage (a mesma conta de armazenamento da fila).
"""
import logging
import os

import azure.functions as func
from azure.cosmos import CosmosClient

from job import executar_job

app = func.FunctionApp()

BANCO = os.environ.get("COSMOS_DATABASE", "jurisprudencia")
CONTAINER = os.environ.get("COSMOS_CONTAINER", "julgados")


def container():
    cliente = CosmosClient.from_connection_string(os.environ["COSMOS_CONNECTION_STRING"])
    return cliente.get_database_client(BANCO).get_container_client(CONTAINER)


@app.queue_trigger(arg_name="mensagem", queue_name="fila-atualizacao", connection="AzureWebJobsStorage")
def atualizar_base(mensagem: func.QueueMessage) -> None:
    logging.info("Verificação iniciada: %s", mensagem.get_body().decode("utf-8", "replace")[:200])
    job = executar_job(container())
    logging.info("Verificação terminou: %s (%s)", job["estado"], job["mensagem"])
# league/services/cartola_client.py
import requests

class CartolaAPIError(Exception):
    pass

def fetch_json(url: str, *, timeout: int = 15):
    r = requests.get(url, timeout=timeout)

    # 204 = sem corpo; não é erro.
    if r.status_code == 204:
        return None

    # Se vier HTML/erro/qualquer coisa fora 200, trate como erro.
    if r.status_code != 200:
        raise CartolaAPIError(f"Cartola API {url} retornou {r.status_code}: {r.text[:200]}")

    # 200 mas corpo vazio (raro) -> trate como None
    if not r.content or not r.text.strip():
        return None

    try:
        return r.json()
    except ValueError as e:
        # ajuda a depurar se o endpoint começou a devolver algo inesperado
        snippet = r.text[:200].replace("\n", " ")
        raise CartolaAPIError(f"Resposta não-JSON em {url}: {snippet}") from e

from urllib.parse import urljoin
import re

URL_INDICE_NRS = (
    "https://www.gov.br/trabalho-e-emprego/pt-br/acesso-a-informacao/"
    "participacao-social/conselhos-e-orgaos-colegiados/"
    "comissao-tripartite-partitaria-permanente/normas-regulamentadora/"
    "normas-regulamentadoras-vigentes"
)

def descobrir_links_nrs() -> dict:
    """
    Acessa a página oficial de NRs vigentes e captura os links reais das NRs.
    Assim o sistema não depende de URLs montadas manualmente.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 monitor-nr-bot/1.0"
    }

    resp = requests.get(URL_INDICE_NRS, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    links = {}

    for a in soup.find_all("a", href=True):
        texto = a.get_text(" ", strip=True)
        href = a["href"]

        # Captura padrões como NR-1, NR 1, NR-01, NR 01 etc.
        match = re.search(r"\bNR[-\s]?0?([1-9]|[1-2][0-9]|3[0-8])\b", texto, re.I)

        if not match:
            # Às vezes o número está só no href do PDF
            match = re.search(r"\bnr[-_]?0?([1-9]|[1-2][0-9]|3[0-8])\b", href, re.I)

        if match:
            numero = int(match.group(1))
            nr = f"NR-{numero:02d}"
            url = urljoin(URL_INDICE_NRS, href)

            # Evita sobrescrever um link bom com links irrelevantes
            if nr not in links:
                links[nr] = url

    return dict(sorted(links.items()))

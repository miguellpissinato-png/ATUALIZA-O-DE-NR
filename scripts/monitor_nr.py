"""
Monitor de Normas Regulamentadoras (NR) - MTE/Gov.br

- Acessa a página oficial de NRs vigentes;
- Descobre automaticamente os links reais das NRs;
- Baixa páginas HTML e PDFs;
- Compara com a última verificação;
- Envia e-mail se encontrar alteração.
"""

import os
import re
import json
import html
import hashlib
import smtplib
import difflib
import requests

from io import BytesIO
from datetime import datetime
from urllib.parse import urljoin, urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup
from pypdf import PdfReader
import anthropic


URL_INDICE_NRS = (
    "https://www.gov.br/trabalho-e-emprego/pt-br/acesso-a-informacao/"
    "participacao-social/conselhos-e-orgaos-colegiados/"
    "comissao-tripartite-partitaria-permanente/normas-regulamentadora/"
    "normas-regulamentadoras-vigentes"
)

PASTA_DATA = "data"
PASTA_CONTEUDOS = os.path.join(PASTA_DATA, "conteudos")

ARQUIVO_HASHES = os.path.join(PASTA_DATA, "hashes.json")
ARQUIVO_LOG = os.path.join(PASTA_DATA, "log.json")
ARQUIVO_LINKS = os.path.join(PASTA_DATA, "links_nrs.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 monitor-nr-bot/2.0"
}

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")


def garantir_pastas():
    os.makedirs(PASTA_DATA, exist_ok=True)
    os.makedirs(PASTA_CONTEUDOS, exist_ok=True)


def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""

    texto = texto.replace("\xa0", " ")
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)

    linhas = [linha.strip() for linha in texto.splitlines()]
    linhas = [linha for linha in linhas if linha]

    return "\n".join(linhas).strip()


def calcular_hash(texto: str) -> str:
    texto = normalizar_texto(texto)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def carregar_json(caminho: str, padrao):
    if os.path.exists(caminho):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return padrao

    return padrao


def salvar_json(caminho: str, dados):
    garantir_pastas()

    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def caminho_conteudo_nr(nr: str) -> str:
    nome = nr.lower().replace("-", "_") + ".txt"
    return os.path.join(PASTA_CONTEUDOS, nome)


def carregar_conteudo_anterior(nr: str) -> str:
    caminho = caminho_conteudo_nr(nr)

    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            return f.read()

    return ""


def salvar_conteudo_nr(nr: str, conteudo: str):
    garantir_pastas()

    with open(caminho_conteudo_nr(nr), "w", encoding="utf-8") as f:
        f.write(conteudo)


def salvar_log(entradas: list):
    log_atual = carregar_json(ARQUIVO_LOG, [])
    log_atual = entradas + log_atual
    salvar_json(ARQUIVO_LOG, log_atual[:300])


def extrair_numero_nr(texto: str) -> str | None:
    if not texto:
        return None

    padroes = [
        r"\bNR\s*[-–—]?\s*(0?[1-9]|[12]\d|3[0-8])\b",
        r"\bNorma\s+Regulamentadora\s*(?:N[oº°.]*)?\s*(0?[1-9]|[12]\d|3[0-8])\b",
        r"\bnr[-_]?0?([1-9]|[12]\d|3[0-8])\b",
    ]

    for padrao in padroes:
        match = re.search(padrao, texto, flags=re.IGNORECASE)

        if match:
            numero = int(match.group(1))
            return f"NR-{numero:02d}"

    return None


def descobrir_links_nrs() -> dict:
    print("  Buscando lista oficial de NRs no site do MTE/Gov.br...")

    resp = requests.get(URL_INDICE_NRS, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    links = {}

    for a in soup.find_all("a", href=True):
        texto = a.get_text(" ", strip=True)
        href = a["href"].strip()

        if not href:
            continue

        url = urljoin(URL_INDICE_NRS, href)
        dominio = urlparse(url).netloc.lower()

        if "gov.br" not in dominio:
            continue

        nr = extrair_numero_nr(texto) or extrair_numero_nr(href)

        if not nr:
            continue

        combinado = f"{texto} {url}".lower()

        if "nr" not in combinado and "norma-regulamentadora" not in combinado:
            continue

        if nr not in links:
            links[nr] = url
        else:
            atual = links[nr].lower()
            novo = url.lower()

            if novo.endswith(".pdf") and not atual.endswith(".pdf"):
                links[nr] = url

    links = dict(sorted(links.items()))

    salvar_json(ARQUIVO_LINKS, links)

    return links


def extrair_texto_pdf(conteudo_pdf: bytes) -> str:
    reader = PdfReader(BytesIO(conteudo_pdf))
    textos = []

    for i, page in enumerate(reader.pages, start=1):
        texto = page.extract_text() or ""

        if texto.strip():
            textos.append(f"\n--- Página {i} ---\n{texto}")

    return normalizar_texto("\n".join(textos))


def extrair_texto_html(html_bruto: str) -> str:
    soup = BeautifulSoup(html_bruto, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", id="parent-fieldname-text")
        or soup.find("div", id="content")
        or soup.find("div", class_="content")
        or soup.body
        or soup
    )

    texto = main.get_text(separator="\n", strip=True)
    return normalizar_texto(texto)


def buscar_conteudo_nr(url: str) -> tuple[str, str | None]:
    """
    Baixa e extrai texto de uma NR.
    Também tenta capturar a data oficial de última modificação informada pelo Gov.br.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=45, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    ⚠️ Erro ao acessar {url}: {e}")
        return "", None

    data_oficial = resp.headers.get("Last-Modified")

    content_type = resp.headers.get("Content-Type", "").lower()
    url_final = resp.url.lower()

    if "application/pdf" in content_type or url_final.endswith(".pdf"):
        try:
            return extrair_texto_pdf(resp.content), data_oficial
        except Exception as e:
            print(f"    ⚠️ Erro ao ler PDF: {e}")
            return "", data_oficial

    try:
        return extrair_texto_html(resp.text), data_oficial
    except Exception as e:
        print(f"    ⚠️ Erro ao ler HTML: {e}")
        return "", data_oficial
    content_type = resp.headers.get("Content-Type", "").lower()
    url_final = resp.url.lower()

    if "application/pdf" in content_type or url_final.endswith(".pdf"):
        try:
            return extrair_texto_pdf(resp.content)
        except Exception as e:
            print(f"    ⚠️ Erro ao ler PDF: {e}")
            return ""

    try:
        return extrair_texto_html(resp.text)
    except Exception as e:
        print(f"    ⚠️ Erro ao ler HTML: {e}")
        return ""


def gerar_diff(texto_antigo: str, texto_novo: str, limite: int = 12000) -> str:
    antigo = normalizar_texto(texto_antigo).splitlines()
    novo = normalizar_texto(texto_novo).splitlines()

    diff = difflib.unified_diff(
        antigo,
        novo,
        fromfile="versao_anterior",
        tofile="versao_atual",
        lineterm="",
        n=3,
    )

    texto_diff = "\n".join(diff)

    if len(texto_diff) > limite:
        texto_diff = texto_diff[:limite] + "\n\n[DIFF CORTADO POR LIMITE DE TAMANHO]"

    return texto_diff


def analisar_alteracao_com_ia(nr: str, url: str, diff: str, texto_atual: str) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        return {
            "resumo": "Alteração detectada, mas a chave ANTHROPIC_API_KEY não foi configurada.",
            "pontos_principais": [],
            "acoes_recomendadas": [
                "Acesse a NR no site oficial do MTE/Gov.br e revise a alteração manualmente."
            ],
            "urgencia": "média",
        }

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""
Você é um especialista brasileiro em Segurança e Saúde no Trabalho.

A {nr} teve alteração detectada no site oficial do MTE/Gov.br.

URL:
{url}

Abaixo está o DIFF entre a versão anterior e a versão atual.
Linhas iniciadas com "-" foram removidas.
Linhas iniciadas com "+" foram adicionadas.

DIFF:
---
{diff if diff.strip() else "[Diff indisponível]"}
---

Trecho da versão atual:
---
{texto_atual[:6000]}
---

Responda SOMENTE em JSON válido, exatamente neste formato:

{{
  "resumo": "Resumo claro e objetivo do que mudou, em 3 a 5 frases.",
  "pontos_principais": ["ponto 1", "ponto 2", "ponto 3"],
  "acoes_recomendadas": ["ação 1", "ação 2", "ação 3"],
  "urgencia": "alta | média | baixa"
}}

Não use markdown.
Não escreva nada fora do JSON.
"""

    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )

        resposta = msg.content[0].text.strip()
        resposta = resposta.replace("```json", "").replace("```", "").strip()

        dados = json.loads(resposta)

        urgencia = str(dados.get("urgencia", "média")).lower().strip()

        if urgencia == "media":
            urgencia = "média"

        if urgencia not in ["alta", "média", "baixa"]:
            urgencia = "média"

        return {
            "resumo": dados.get("resumo", "Alteração detectada."),
            "pontos_principais": dados.get("pontos_principais", []),
            "acoes_recomendadas": dados.get("acoes_recomendadas", []),
            "urgencia": urgencia,
        }

    except Exception as e:
        print(f"    ⚠️ Erro na análise com IA: {e}")

        return {
            "resumo": "Alteração detectada. A análise automática com IA falhou.",
            "pontos_principais": [],
            "acoes_recomendadas": [
                "Acesse a NR no site oficial do MTE/Gov.br e compare manualmente."
            ],
            "urgencia": "média",
        }


def montar_email_html(alteracoes: list) -> str:
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    cores = {
        "alta": "#E24B4A",
        "média": "#EF9F27",
        "baixa": "#1D9E75",
    }

    blocos = ""

    for alt in alteracoes:
        analise = alt["analise"]
        urgencia = analise.get("urgencia", "média")
        cor = cores.get(urgencia, "#EF9F27")

        nr = html.escape(alt["nr"])
        url = html.escape(alt["url"])
        resumo = html.escape(analise.get("resumo", ""))

        pontos = "".join(
            f"<li>{html.escape(str(p))}</li>"
            for p in analise.get("pontos_principais", [])
        )

        acoes = "".join(
            f"<li>{html.escape(str(a))}</li>"
            for a in analise.get("acoes_recomendadas", [])
        )

        blocos += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-left:4px solid {cor};
                    border-radius:8px;padding:20px;margin-bottom:16px;">
          <h2 style="margin:0 0 8px;color:#111827;font-size:18px;">{nr}</h2>

          <p style="margin:0 0 10px;font-size:13px;color:#ffffff;background:{cor};
                    display:inline-block;padding:4px 10px;border-radius:20px;">
            Urgência {html.escape(urgencia).upper()}
          </p>

          <p style="font-size:14px;color:#374151;line-height:1.6;">
            {resumo}
          </p>

          {f'<p><strong>Pontos principais:</strong></p><ul>{pontos}</ul>' if pontos else ''}
          {f'<p><strong>Ações recomendadas:</strong></p><ul>{acoes}</ul>' if acoes else ''}

          <p style="font-size:13px;">
            🔗 <a href="{url}">Acessar NR no site oficial</a>
          </p>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="UTF-8">
</head>
<body style="font-family:Arial,sans-serif;background:#f3f4f6;margin:0;padding:20px;">
  <div style="max-width:700px;margin:0 auto;">
    <div style="background:#1e293b;padding:24px;border-radius:12px 12px 0 0;">
      <h1 style="color:white;margin:0;font-size:22px;">🛡️ Monitor de NRs</h1>
      <p style="color:#cbd5e1;margin:6px 0 0;">Alterações detectadas em {data_hoje}</p>
    </div>

    <div style="background:#dbeafe;border-left:4px solid #3b82f6;padding:16px;color:#1e40af;">
      <strong>{len(alteracoes)} NR(s) com alteração detectada.</strong>
    </div>

    <div style="background:#f8fafc;padding:20px;">
      {blocos}
    </div>

    <div style="background:#1e293b;padding:16px;border-radius:0 0 12px 12px;text-align:center;color:#94a3b8;font-size:12px;">
      Monitor automático de NRs • Fonte: MTE/Gov.br
    </div>
  </div>
</body>
</html>"""


def enviar_email(alteracoes: list):
    remetente = os.getenv("EMAIL_REMETENTE")
    senha = os.getenv("EMAIL_SENHA_APP")
    destinatario = os.getenv("EMAIL_DESTINATARIO")

    if not remetente or not senha or not destinatario:
        print("  ⚠️ E-mail não enviado: configure EMAIL_REMETENTE, EMAIL_SENHA_APP e EMAIL_DESTINATARIO.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 [{len(alteracoes)} NR(s) alterada(s)] Monitor de NRs — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = remetente
    msg["To"] = destinatario

    msg.attach(MIMEText(montar_email_html(alteracoes), "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(remetente, senha)
            smtp.sendmail(remetente, destinatario, msg.as_string())

        print(f"  ✉️ E-mail enviado para {destinatario}")

    except Exception as e:
        print(f"  ⚠️ Erro ao enviar e-mail: {e}")


def main():
    garantir_pastas()

    print(f"\n{'=' * 65}")
    print(f"  Monitor de NRs — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'=' * 65}\n")

    try:
        nrs_monitoradas = descobrir_links_nrs()
    except Exception as e:
        print(f"  ❌ Erro ao descobrir links das NRs: {e}")
        return

    if not nrs_monitoradas:
        print("  ❌ Nenhuma NR foi encontrada.")
        return

    print(f"  {len(nrs_monitoradas)} NRs encontradas para monitoramento.\n")

    hashes_salvos = carregar_json(ARQUIVO_HASHES, {})
    hashes_atualizados = dict(hashes_salvos)

    alteracoes = []
    log_entrada = []

    for nr, url in nrs_monitoradas.items():
        print(f"  Verificando {nr}...")
        print(f"    URL: {url}")

        conteudo_atual = buscar_conteudo_nr(url)

        if not conteudo_atual:
            print(f"    ⚠️ Sem conteúdo para {nr}. Pulando.")
            log_entrada.append({
                "nr": nr,
                "evento": "erro_sem_conteudo",
                "url": url,
                "data": datetime.now().isoformat(),
            })
            continue

        hash_atual = calcular_hash(conteudo_atual)
        hash_anterior = hashes_salvos.get(nr)

        if not hash_anterior:
            print(f"    📝 Primeiro registro de {nr}. Nenhum alerta enviado.")

            hashes_atualizados[nr] = hash_atual
            salvar_conteudo_nr(nr, conteudo_atual)

            log_entrada.append({
                "nr": nr,
                "evento": "primeiro_registro",
                "url": url,
                "data": datetime.now().isoformat(),
            })

        elif hash_anterior != hash_atual:
            print(f"    🔴 Alteração detectada em {nr}!")

            conteudo_anterior = carregar_conteudo_anterior(nr)
            diff = gerar_diff(conteudo_anterior, conteudo_atual)

            analise = analisar_alteracao_com_ia(nr, url, diff, conteudo_atual)

            alteracoes.append({
                "nr": nr,
                "url": url,
                "analise": analise,
            })

            hashes_atualizados[nr] = hash_atual
            salvar_conteudo_nr(nr, conteudo_atual)

            log_entrada.append({
                "nr": nr,
                "evento": "alteracao_detectada",
                "url": url,
                "data": datetime.now().isoformat(),
                "urgencia": analise.get("urgencia"),
                "resumo": analise.get("resumo"),
            })

        else:
            print(f"    ✅ {nr} sem alterações.")

            if not os.path.exists(caminho_conteudo_nr(nr)):
                salvar_conteudo_nr(nr, conteudo_atual)

    salvar_json(ARQUIVO_HASHES, hashes_atualizados)

    if log_entrada:
        salvar_log(log_entrada)

    if alteracoes:
        print(f"\n  Enviando e-mail com {len(alteracoes)} alteração(ões)...")
        enviar_email(alteracoes)
    else:
        print("\n  ✅ Nenhuma alteração detectada. Nenhum e-mail enviado.")

    print(f"\n{'=' * 65}")
    print(f"  Verificação concluída. {len(alteracoes)} alteração(ões) encontrada(s).")
    print(f"{'=' * 65}\n")


if __name__ == "__main__":
    main()

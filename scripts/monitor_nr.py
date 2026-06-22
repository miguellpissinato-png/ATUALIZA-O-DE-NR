"""
Monitor de Normas Regulamentadoras (NR) - MTE/Gov.br

- Acessa a página oficial de NRs vigentes;
- Descobre automaticamente os links reais das NRs;
- Baixa páginas HTML e PDFs;
- Compara com a última verificação;
- Envia e-mail se encontrar alteração;
- Gera um arquivo status_nrs.json para alimentar o painel do site.

Observação importante:
O site gov.br ocasionalmente BLOQUEIA conexões vindas de servidores/
datacenters (como os runners do GitHub Actions), retornando erro de
rede ("Network is unreachable") mesmo que a URL esteja correta e
acessível normalmente de uma conexão residencial.

Para contornar isso, todas as requisições passam primeiro por um
proxy (ScraperAPI, via variável de ambiente SCRAPERAPI_KEY). Se essa
chave não estiver configurada, o script tenta acesso direto (que pode
falhar intermitentemente).
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

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
ARQUIVO_STATUS_NRS = os.path.join(PASTA_DATA, "status_nrs.json")
ARQUIVO_STATUS_MONITOR = os.path.join(PASTA_DATA, "status_monitor.json")

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) monitor-nr-bot/2.1"
}

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# Número de tentativas para cada requisição antes de desistir
MAX_TENTATIVAS = 3


# ── Requisições HTTP (com proxy de fallback) ───────────────────────────────────

def _fazer_requisicao(url: str):
    """
    Faz uma requisição GET com retentativas.

    Tenta, na ordem:
    1. Via ScraperAPI (se SCRAPERAPI_KEY estiver configurada) — contorna
       bloqueios de IP de datacenter.
    2. Acesso direto, como fallback.

    Levanta a última exceção se todas as tentativas falharem.
    """
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    ultima_excecao = None

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            if scraperapi_key:
                resp = requests.get(
                    "https://api.scraperapi.com/",
                    params={
                        "api_key": scraperapi_key,
                        "url": url,
                        "country_code": "br",
                    },
                    timeout=60,
                )
            else:
                resp = requests.get(
                    url, headers=HEADERS, timeout=30, allow_redirects=True
                )

            resp.raise_for_status()
            return resp

        except requests.RequestException as e:
            ultima_excecao = e
            print(f"    ⚠️ Tentativa {tentativa}/{MAX_TENTATIVAS} falhou para {url}: {e}")

    raise ultima_excecao


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


def agora_brasilia():
    return datetime.now(FUSO_BRASIL)


def formatar_data_hora_br(data: datetime) -> str:
    return data.strftime("%d/%m/%Y, %H:%M")


def evento_e_alteracao_real(evento) -> bool:
    """
    O log.json deve alimentar a área "Eventos recentes" do site.

    Por isso, ele deve guardar SOMENTE alterações reais em NRs.
    Verificação concluída, primeiro registro e erro de leitura ficam no status,
    não entram em eventos recentes.
    """
    return (
        isinstance(evento, dict)
        and evento.get("evento") == "alteracao_detectada"
        and bool(evento.get("nr"))
    )


def salvar_log(entradas: list):
    """
    Mantém o log.json limpo.

    Antes o script gravava primeiro_registro/erro_sem_conteudo no log,
    e o site acabava mostrando "NR — Evento" mesmo sem alteração real.
    Agora o log recebe apenas alterações detectadas.
    """
    log_atual = carregar_json(ARQUIVO_LOG, [])

    if not isinstance(log_atual, list):
        log_atual = []

    entradas_validas = [e for e in entradas if evento_e_alteracao_real(e)]
    log_atual_limpo = [e for e in log_atual if evento_e_alteracao_real(e)]

    log_atualizado = entradas_validas + log_atual_limpo
    salvar_json(ARQUIVO_LOG, log_atualizado[:300])


def salvar_status_monitor(status: str, mensagem: str, **extras):
    """
    Arquivo global para o site saber quando o robô rodou pela última vez
    e quantas alterações foram encontradas nesta verificação.
    """
    agora = agora_brasilia()

    dados = {
        "ultima_verificacao": agora.isoformat(),
        "ultima_verificacao_formatada": formatar_data_hora_br(agora),
        "status": status,
        "mensagem": mensagem,
    }

    dados.update(extras)
    salvar_json(ARQUIVO_STATUS_MONITOR, dados)


def formatar_data_oficial(data_http):
    """
    Converte a data Last-Modified do servidor para ISO.
    Se o Gov.br não informar essa data, retorna None.
    """
    if not data_http:
        return None

    try:
        data = parsedate_to_datetime(data_http)
        return data.isoformat()
    except Exception:
        return data_http


def converter_data_pt_para_iso(data_texto: str):
    """
    Converte datas brasileiras para ISO.
    Aceita:
    - 12/06/2026
    - 12-06-2026
    - 12 de junho de 2026
    """
    if not data_texto:
        return None

    data_texto = data_texto.strip().lower()

    meses = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
        "abril": 4, "maio": 5, "junho": 6, "julho": 7,
        "agosto": 8, "setembro": 9, "outubro": 10,
        "novembro": 11, "dezembro": 12,
    }

    match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", data_texto)

    if match:
        dia = int(match.group(1))
        mes = int(match.group(2))
        ano = int(match.group(3))

        try:
            return datetime(ano, mes, dia).isoformat()
        except ValueError:
            return None

    match = re.search(
        r"(\d{1,2})\s+de\s+([a-zçãé]+)\s+de\s+(\d{4})",
        data_texto,
        flags=re.IGNORECASE,
    )

    if match:
        dia = int(match.group(1))
        mes_nome = match.group(2).lower()
        ano = int(match.group(3))

        mes = meses.get(mes_nome)

        if not mes:
            return None

        try:
            return datetime(ano, mes, dia).isoformat()
        except ValueError:
            return None

    return None


def extrair_data_do_texto_nr(texto: str):
    """
    Tenta encontrar no texto da página/PDF uma data provável de publicação,
    atualização, modificação ou alteração da NR.

    Se encontrar várias datas, retorna a mais recente.
    """
    if not texto:
        return None

    candidatos = []

    padroes = [
        r"(?:atualizado em|atualizada em|publicado em|publicada em|última atualização|ultima atualização|última modificação|ultima modificação|modificado em|modificada em)\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"(?:atualizado em|atualizada em|publicado em|publicada em|última atualização|ultima atualização|última modificação|ultima modificação|modificado em|modificada em)\s*:?\s*(\d{1,2}\s+de\s+[a-zçãé]+\s+de\s+\d{4})",
        r"(?:portaria|decreto|lei|instrução normativa|instrucao normativa|resolução|resolucao)[^\n]{0,180}?,\s*de\s*(\d{1,2}\s+de\s+[a-zçãé]+\s+de\s+\d{4})",
        r"(?:portaria|decreto|lei|instrução normativa|instrucao normativa|resolução|resolucao)[^\n]{0,180}?,\s*de\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"(?:alterada|alterado|atualizada|atualizado|redação dada|redacao dada)[^\n]{0,220}?(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"(?:alterada|alterado|atualizada|atualizado|redação dada|redacao dada)[^\n]{0,220}?(\d{1,2}\s+de\s+[a-zçãé]+\s+de\s+\d{4})",
    ]

    for padrao in padroes:
        matches = re.findall(padrao, texto, flags=re.IGNORECASE)

        for data_encontrada in matches:
            data_iso = converter_data_pt_para_iso(data_encontrada)

            if data_iso:
                candidatos.append(data_iso)

    if not candidatos:
        return None

    candidatos_ordenados = sorted(candidatos, reverse=True)
    return candidatos_ordenados[0]


def extrair_numero_nr(texto: str):
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

    resp = _fazer_requisicao(URL_INDICE_NRS)

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


def buscar_conteudo_nr(url: str):
    """
    Baixa e extrai texto de uma NR.

    Tenta descobrir a data oficial de duas formas:
    1. Pelo cabeçalho Last-Modified do Gov.br;
    2. Pelo próprio texto da página/PDF, procurando datas de atualização,
       publicação, modificação, alteração ou portarias.
    """
    try:
        resp = _fazer_requisicao(url)
    except requests.RequestException as e:
        print(f"    ⚠️ Erro ao acessar {url}: {e}")
        return "", None

    data_last_modified = formatar_data_oficial(resp.headers.get("Last-Modified"))

    content_type = resp.headers.get("Content-Type", "").lower()
    url_final = resp.url.lower()

    conteudo = ""

    if "application/pdf" in content_type or url_final.endswith(".pdf"):
        try:
            conteudo = extrair_texto_pdf(resp.content)
        except Exception as e:
            print(f"    ⚠️ Erro ao ler PDF: {e}")
            return "", data_last_modified
    else:
        try:
            conteudo = extrair_texto_html(resp.text)
        except Exception as e:
            print(f"    ⚠️ Erro ao ler HTML: {e}")
            return "", data_last_modified

    data_extraida_do_texto = extrair_data_do_texto_nr(conteudo)
    data_oficial = data_extraida_do_texto or data_last_modified

    return conteudo, data_oficial


def gerar_diff(texto_antigo: str, texto_novo: str, limite: int = 12000) -> str:
    antigo = normalizar_texto(texto_antigo).splitlines()
    novo = normalizar_texto(texto_novo).splitlines()

    diff = difflib.unified_diff(
        antigo, novo,
        fromfile="versao_anterior", tofile="versao_atual",
        lineterm="", n=3,
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

    cores = {"alta": "#E24B4A", "média": "#EF9F27", "baixa": "#1D9E75"}

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

        data_oficial = alt.get("data_oficial")
        data_oficial_html = ""

        if data_oficial:
            data_oficial_html = f"""
              <p style="font-size:13px;color:#6b7280;margin:8px 0;">
                Data encontrada no Gov.br/PDF: {html.escape(str(data_oficial))}
              </p>
            """

        blocos += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-left:4px solid {cor};
                    border-radius:8px;padding:20px;margin-bottom:16px;">
          <h2 style="margin:0 0 8px;color:#111827;font-size:18px;">{nr}</h2>

          <p style="margin:0 0 10px;font-size:13px;color:#ffffff;background:{cor};
                    display:inline-block;padding:4px 10px;border-radius:20px;">
            Urgência {html.escape(urgencia).upper()}
          </p>

          {data_oficial_html}

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


def enviar_email_erro_critico(mensagem: str):
    """
    Envia um aviso por e-mail quando o sistema não consegue nem descobrir
    os links das NRs (ex: bloqueio persistente do gov.br), para que o
    problema não passe despercebido por dias.
    """
    remetente = os.getenv("EMAIL_REMETENTE")
    senha = os.getenv("EMAIL_SENHA_APP")
    destinatario = os.getenv("EMAIL_DESTINATARIO")

    if not remetente or not senha or not destinatario:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ Monitor de NRs — falha na verificação de {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = remetente
    msg["To"] = destinatario

    corpo = f"""<p>O monitor de NRs não conseguiu completar a verificação de hoje.</p>
    <p><strong>Motivo:</strong> {html.escape(mensagem)}</p>
    <p>Isso geralmente ocorre por bloqueio temporário do site gov.br a conexões
    de servidores. Nenhuma ação é necessária se isso for pontual; caso se repita
    por vários dias, verifique a configuração do SCRAPERAPI_KEY.</p>"""

    msg.attach(MIMEText(corpo, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(remetente, senha)
            smtp.sendmail(remetente, destinatario, msg.as_string())
        print(f"  ✉️ E-mail de aviso de erro enviado para {destinatario}")
    except Exception as e:
        print(f"  ⚠️ Erro ao enviar e-mail de aviso: {e}")



def main():
    garantir_pastas()

    inicio_verificacao = agora_brasilia()
    inicio_iso = inicio_verificacao.isoformat()
    inicio_formatado = formatar_data_hora_br(inicio_verificacao)

    print(f"\n{'=' * 65}")
    print(f"  Monitor de NRs — {inicio_formatado}")
    print(f"{'=' * 65}\n")

    try:
        nrs_monitoradas = descobrir_links_nrs()
    except Exception as e:
        mensagem = f"Erro ao descobrir links das NRs: {e}"
        print(f"  ❌ {mensagem}")

        salvar_status_monitor(
            status="erro",
            mensagem=mensagem,
            houve_alteracao=False,
            alteracoes_desde_ultima_verificacao=0,
            nrs_monitoradas=0,
            erros=1,
        )
        salvar_log([])
        enviar_email_erro_critico(str(e))
        return

    if not nrs_monitoradas:
        mensagem = "A página índice de NRs não retornou nenhum link reconhecido."
        print(f"  ❌ {mensagem}")

        salvar_status_monitor(
            status="erro",
            mensagem=mensagem,
            houve_alteracao=False,
            alteracoes_desde_ultima_verificacao=0,
            nrs_monitoradas=0,
            erros=1,
        )
        salvar_log([])
        enviar_email_erro_critico(mensagem)
        return

    print(f"  {len(nrs_monitoradas)} NRs encontradas para monitoramento.\n")

    hashes_salvos = carregar_json(ARQUIVO_HASHES, {})

    if not isinstance(hashes_salvos, dict):
        hashes_salvos = {}

    hashes_atualizados = dict(hashes_salvos)

    alteracoes = []
    log_alteracoes = []
    status_nrs = []

    for nr, url in nrs_monitoradas.items():
        print(f"  Verificando {nr}...")
        print(f"    URL: {url}")

        momento_nr = agora_brasilia()
        momento_verificacao = momento_nr.isoformat()
        momento_verificacao_formatado = formatar_data_hora_br(momento_nr)

        conteudo_atual, data_oficial = buscar_conteudo_nr(url)

        if not conteudo_atual:
            print(f"    ⚠️ Sem conteúdo para {nr}. Pulando.")

            status_nrs.append({
                "nr": nr,
                "url": url,
                "status": "erro_sem_conteudo",
                "evento": "Erro de leitura",
                "data_oficial": data_oficial,
                "data_encontrada": data_oficial,
                "ultima_verificacao": momento_verificacao,
                "ultima_verificacao_formatada": momento_verificacao_formatado,
            })

            continue

        hash_atual = calcular_hash(conteudo_atual)
        hash_anterior = hashes_salvos.get(nr)

        if not hash_anterior:
            print(f"    📝 Primeiro registro de {nr}. Nenhum alerta enviado.")

            hashes_atualizados[nr] = hash_atual
            salvar_conteudo_nr(nr, conteudo_atual)

            status_nrs.append({
                "nr": nr,
                "url": url,
                "status": "primeiro_registro",
                "evento": "Primeiro registro",
                "data_oficial": data_oficial,
                "data_encontrada": data_oficial,
                "ultima_verificacao": momento_verificacao,
                "ultima_verificacao_formatada": momento_verificacao_formatado,
            })

        elif hash_anterior != hash_atual:
            print(f"    🔴 Alteração detectada em {nr}!")

            conteudo_anterior = carregar_conteudo_anterior(nr)
            diff = gerar_diff(conteudo_anterior, conteudo_atual)

            analise = analisar_alteracao_com_ia(nr, url, diff, conteudo_atual)

            alteracao = {
                "nr": nr,
                "url": url,
                "data_oficial": data_oficial,
                "data_encontrada": data_oficial,
                "analise": analise,
            }

            alteracoes.append(alteracao)

            hashes_atualizados[nr] = hash_atual
            salvar_conteudo_nr(nr, conteudo_atual)

            resumo = analise.get("resumo", "Alteração detectada.")
            urgencia = analise.get("urgencia", "média")

            log_alteracoes.append({
                "nr": nr,
                "evento": "alteracao_detectada",
                "evento_label": "Alteração detectada",
                "titulo": f"{nr} — Alteração detectada",
                "descricao": resumo,
                "url": url,
                "data": momento_verificacao,
                "detectado_em": momento_verificacao,
                "detectado_em_formatado": momento_verificacao_formatado,
                "data_oficial": data_oficial,
                "data_encontrada": data_oficial,
                "urgencia": urgencia,
                "resumo": resumo,
                "pontos_principais": analise.get("pontos_principais", []),
                "acoes_recomendadas": analise.get("acoes_recomendadas", []),
            })

            status_nrs.append({
                "nr": nr,
                "url": url,
                "status": "alteracao_detectada",
                "evento": "Alteração detectada",
                "data_oficial": data_oficial,
                "data_encontrada": data_oficial,
                "ultima_verificacao": momento_verificacao,
                "ultima_verificacao_formatada": momento_verificacao_formatado,
                "urgencia": urgencia,
                "resumo": resumo,
            })

        else:
            print(f"    ✅ {nr} sem alterações.")

            if not os.path.exists(caminho_conteudo_nr(nr)):
                salvar_conteudo_nr(nr, conteudo_atual)

            status_nrs.append({
                "nr": nr,
                "url": url,
                "status": "sem_alteracoes",
                "evento": "Sem alterações",
                "data_oficial": data_oficial,
                "data_encontrada": data_oficial,
                "ultima_verificacao": momento_verificacao,
                "ultima_verificacao_formatada": momento_verificacao_formatado,
            })

    fim_verificacao = agora_brasilia()
    fim_iso = fim_verificacao.isoformat()
    fim_formatado = formatar_data_hora_br(fim_verificacao)

    total_erros = sum(1 for item in status_nrs if str(item.get("status", "")).startswith("erro"))
    total_alteracoes = len(alteracoes)
    houve_alteracao = total_alteracoes > 0

    if houve_alteracao:
        mensagem_status = f"{total_alteracoes} NR(s) mudaram desde a última verificação."
    else:
        mensagem_status = "Nenhuma NR mudou desde a última verificação."

    status_geral = "concluida_com_erros" if total_erros else "concluida"

    # Mantém status_nrs.json como lista, para não quebrar seu site caso ele já leia esse formato.
    # Também adiciona campos globais em cada item, facilitando a exibição da última verificação.
    for item in status_nrs:
        item["inicio_verificacao"] = inicio_iso
        item["inicio_verificacao_formatada"] = inicio_formatado
        item["fim_verificacao"] = fim_iso
        item["fim_verificacao_formatada"] = fim_formatado
        item["ultima_verificacao_geral"] = fim_iso
        item["ultima_verificacao_geral_formatada"] = fim_formatado
        item["status_sistema"] = status_geral
        item["houve_alteracao_na_verificacao"] = houve_alteracao
        item["alteracoes_desde_ultima_verificacao"] = total_alteracoes
        item["mensagem_verificacao"] = mensagem_status

    salvar_json(ARQUIVO_HASHES, hashes_atualizados)
    salvar_json(ARQUIVO_STATUS_NRS, status_nrs)

    salvar_status_monitor(
        status=status_geral,
        mensagem=mensagem_status,
        houve_alteracao=houve_alteracao,
        alteracoes_desde_ultima_verificacao=total_alteracoes,
        nrs_monitoradas=len(nrs_monitoradas),
        nrs_verificadas=len(status_nrs),
        erros=total_erros,
        inicio_verificacao=inicio_iso,
        inicio_verificacao_formatada=inicio_formatado,
        fim_verificacao=fim_iso,
        fim_verificacao_formatada=fim_formatado,
    )

    # Atualiza/limpa log.json. Ele fica somente com alterações reais.
    salvar_log(log_alteracoes)

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

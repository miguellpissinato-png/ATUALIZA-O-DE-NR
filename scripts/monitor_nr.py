"""
Monitor de Normas Regulamentadoras (NR) - MTE
Detecta alterações e envia resumo por e-mail usando IA (Claude)
"""

import os
import json
import hashlib
import smtplib
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
import anthropic

# ── Configurações ──────────────────────────────────────────────────────────────

# URL base atual do MTE para as NRs vigentes
_BASE_URL = ("https://www.gov.br/trabalho-e-emprego/pt-br/acesso-a-informacao/"
             "participacao-social/conselhos-e-orgaos-colegiados/"
             "comissao-tripartite-partitaria-permanente/normas-regulamentadora/"
             "normas-regulamentadoras-vigentes/norma-regulamentadora-no-{n}-nr-{n}")

# NRs a monitorar (NR-01 a NR-38).
# Obs: NR-02 e NR-27 estão revogadas, mas mantemos no monitoramento
# (sem problema, o conteúdo da página simplesmente não deve mudar).
NRS_MONITORADAS = {
    f"NR-{i:02d}": _BASE_URL.format(n=i)
    for i in range(1, 39)
}

ARQUIVO_HASHES = "data/hashes.json"
ARQUIVO_LOG    = "data/log.json"

# ── Funções de scraping ────────────────────────────────────────────────────────

def buscar_conteudo_nr(url: str) -> str:
    """Baixa e extrai o texto principal da página de uma NR."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) monitor-nr-bot/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Erro ao acessar {url}: {e}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove menus, scripts e rodapés
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # Tenta vários seletores comuns do portal gov.br, do mais específico ao mais genérico
    main = (
        soup.find("div", id="parent-fieldname-text")
        or soup.find("div", class_="documentByLine")
        or soup.find("main")
        or soup.find("article")
        or soup.find("div", class_="content")
        or soup.find("div", id="content")
    )
    texto = (main or soup).get_text(separator="\n", strip=True)

    # Remove linhas muito curtas (menus, breadcrumbs etc.)
    linhas = [l for l in texto.splitlines() if len(l.strip()) > 40]
    return "\n".join(linhas)


def calcular_hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ── Funções de persistência ────────────────────────────────────────────────────

def carregar_hashes() -> dict:
    if os.path.exists(ARQUIVO_HASHES):
        with open(ARQUIVO_HASHES, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_hashes(hashes: dict):
    os.makedirs("data", exist_ok=True)
    with open(ARQUIVO_HASHES, "w", encoding="utf-8") as f:
        json.dump(hashes, f, ensure_ascii=False, indent=2)


def salvar_log(entradas: list):
    os.makedirs("data", exist_ok=True)
    log_existente = []
    if os.path.exists(ARQUIVO_LOG):
        with open(ARQUIVO_LOG, "r", encoding="utf-8") as f:
            log_existente = json.load(f)
    log_existente = entradas + log_existente  # mais recentes primeiro
    # Mantém apenas os últimos 200 registros
    with open(ARQUIVO_LOG, "w", encoding="utf-8") as f:
        json.dump(log_existente[:200], f, ensure_ascii=False, indent=2)


# ── IA: análise da alteração ───────────────────────────────────────────────────

def analisar_alteracao_com_ia(nr: str, texto_atual: str) -> dict:
    """
    Usa o Claude para resumir o que mudou e recomendar ações.
    Retorna dict com 'resumo' e 'acoes'.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Você é um especialista em segurança do trabalho brasileiro.

A norma regulamentadora {nr} foi atualizada. Abaixo está o conteúdo atual da página oficial:

---
{texto_atual[:6000]}
---

Com base nesse conteúdo, responda em JSON com EXATAMENTE este formato (sem markdown, sem explicações extras):
{{
  "resumo": "Resumo claro e objetivo do que mudou ou do conteúdo principal desta NR, em 3-5 frases.",
  "pontos_principais": ["ponto 1", "ponto 2", "ponto 3"],
  "acoes_recomendadas": ["ação 1", "ação 2", "ação 3"],
  "urgencia": "alta | média | baixa"
}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = msg.content[0].text.strip()
        # Remove possíveis blocos markdown
        texto = texto.replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except Exception as e:
        print(f"  Erro na análise IA: {e}")
        return {
            "resumo": "Alteração detectada. Análise automática indisponível.",
            "pontos_principais": [],
            "acoes_recomendadas": ["Acesse a NR no site do MTE para verificar as mudanças."],
            "urgencia": "média",
        }


# ── E-mail ─────────────────────────────────────────────────────────────────────

def montar_email_html(alteracoes: list) -> str:
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    cor_urgencia = {"alta": "#E24B4A", "média": "#EF9F27", "baixa": "#1D9E75"}

    blocos_nr = ""
    for alt in alteracoes:
        cor = cor_urgencia.get(alt["analise"].get("urgencia", "média"), "#EF9F27")
        pontos = "".join(f"<li>{p}</li>" for p in alt["analise"].get("pontos_principais", []))
        acoes  = "".join(f"<li>{a}</li>" for a in alt["analise"].get("acoes_recomendadas", []))

        blocos_nr += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-left:4px solid {cor};
                    border-radius:8px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
            <h2 style="margin:0;font-size:16px;color:#111827;">{alt['nr']}</h2>
            <span style="background:{cor};color:#fff;font-size:11px;padding:3px 10px;
                         border-radius:20px;font-weight:600;">
              Urgência {alt['analise'].get('urgencia','média').upper()}
            </span>
          </div>
          <p style="font-size:14px;color:#374151;line-height:1.6;margin:0 0 12px;">
            {alt['analise']['resumo']}
          </p>
          {'<p style="font-weight:600;font-size:13px;color:#111827;margin:0 0 4px;">Pontos principais:</p><ul style="font-size:13px;color:#374151;margin:0 0 12px;padding-left:18px;">' + pontos + '</ul>' if pontos else ''}
          {'<p style="font-weight:600;font-size:13px;color:#111827;margin:0 0 4px;">Ações recomendadas:</p><ul style="font-size:13px;color:#374151;margin:0;padding-left:18px;">' + acoes + '</ul>' if acoes else ''}
          <p style="font-size:12px;color:#9ca3af;margin:12px 0 0;">
            🔗 <a href="{alt['url']}" style="color:#3b82f6;">Acessar NR no site do MTE</a>
          </p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;background:#f3f4f6;margin:0;padding:20px;">
  <div style="max-width:600px;margin:0 auto;">

    <div style="background:#1e293b;border-radius:12px 12px 0 0;padding:24px;">
      <h1 style="color:#ffffff;margin:0;font-size:20px;">🛡️ Monitor de NRs</h1>
      <p style="color:#94a3b8;margin:4px 0 0;font-size:13px;">
        Alterações detectadas em {data_hoje}
      </p>
    </div>

    <div style="background:#dbeafe;border-left:4px solid #3b82f6;padding:14px 20px;
                font-size:14px;color:#1e40af;">
      <strong>{len(alteracoes)} NR(s) com alteração detectada</strong> nesta verificação diária.
      Revise os itens abaixo e tome as ações necessárias.
    </div>

    <div style="background:#f8fafc;padding:20px;">
      {blocos_nr}
    </div>

    <div style="background:#1e293b;border-radius:0 0 12px 12px;padding:16px 24px;
                font-size:12px;color:#94a3b8;text-align:center;">
      Monitor automático de NRs • Verificação diária às 08:00 •
      <a href="https://www.gov.br/trabalho-e-emprego/pt-br" style="color:#60a5fa;">Site MTE</a>
    </div>

  </div>
</body>
</html>"""


def enviar_email(alteracoes: list):
    """Envia o e-mail de alerta via SMTP (Gmail)."""
    remetente  = os.environ["EMAIL_REMETENTE"]   # seu Gmail
    senha      = os.environ["EMAIL_SENHA_APP"]   # senha de app do Gmail
    destinatario = os.environ["EMAIL_DESTINATARIO"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 [{len(alteracoes)} NR(s) alterada(s)] Monitor de NRs — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"]    = remetente
    msg["To"]      = destinatario

    html = montar_email_html(alteracoes)
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(remetente, senha)
        smtp.sendmail(remetente, destinatario, msg.as_string())

    print(f"  ✉️  E-mail enviado para {destinatario}")


# ── Loop principal ─────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"  Monitor de NRs — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*55}\n")

    hashes_salvos = carregar_hashes()
    hashes_novos  = {}
    alteracoes    = []
    log_entrada   = []

    for nr, url in NRS_MONITORADAS.items():
        print(f"  Verificando {nr}...")
        conteudo = buscar_conteudo_nr(url)

        if not conteudo:
            print(f"  ⚠️  Sem conteúdo para {nr}, pulando.")
            continue

        hash_atual = calcular_hash(conteudo)
        hashes_novos[nr] = hash_atual

        if nr not in hashes_salvos:
            # Primeira execução — apenas registra
            print(f"  📝 Primeiro registro de {nr}.")
            log_entrada.append({"nr": nr, "evento": "primeiro_registro",
                                  "data": datetime.now().isoformat()})
        elif hashes_salvos[nr] != hash_atual:
            print(f"  🔴 ALTERAÇÃO DETECTADA em {nr}! Analisando com IA...")
            analise = analisar_alteracao_com_ia(nr, conteudo)
            alteracoes.append({"nr": nr, "url": url, "analise": analise})
            log_entrada.append({
                "nr": nr, "evento": "alteracao_detectada",
                "data": datetime.now().isoformat(),
                "urgencia": analise.get("urgencia"),
                "resumo": analise.get("resumo"),
            })
            print(f"     Urgência: {analise.get('urgencia', '?')} | {analise['resumo'][:80]}...")
        else:
            print(f"  ✅ {nr} sem alterações.")

    # Salva hashes e log
    salvar_hashes(hashes_novos)
    if log_entrada:
        salvar_log(log_entrada)

    # Envia e-mail se houver alterações
    if alteracoes:
        print(f"\n  Enviando e-mail com {len(alteracoes)} alteração(ões)...")
        enviar_email(alteracoes)
    else:
        print("\n  ✅ Nenhuma alteração detectada. Nenhum e-mail enviado.")

    print(f"\n{'='*55}")
    print(f"  Verificação concluída. {len(alteracoes)} alteração(ões) encontrada(s).")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()

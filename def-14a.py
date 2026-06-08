"""
Scraping da tabela 'Security Ownership of Certain Beneficial Owners and
Management' a partir de um DEF-14A do SEC EDGAR.
"""
import re
import sys
import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

HEADERS = {
    "User-Agent": "insider-trading-research vitorsaito95@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_URL = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000036/nvda-20260512.htm"
SECTION_KEYWORDS = [
    "security ownership of certain beneficial owners",
    "security ownership",
]


def fetch_html(url):
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.text


def find_section_headings(soup):
    """Retorna todos os elementos que contêm o texto da seção de ownership."""
    candidates = []
    pattern = re.compile(SECTION_KEYWORDS[0], re.IGNORECASE)
    for txt_node in soup.find_all(string=pattern):
        parent = txt_node.parent
        while parent and parent.name in ("span", "b", "strong", "i", "em", "u", "font"):
            parent = parent.parent
        if parent:
            candidates.append((parent, txt_node.strip()))
    return candidates


def looks_like_ownership_table(table):
    """Valida se uma tabela tem formato de ownership: colunas Name + Shares + Percent."""
    rows = table.find_all("tr")
    if len(rows) < 2:
        return False
    # Junta o texto das primeiras linhas (cabeçalho geralmente está aí)
    header_text = " ".join(
        c.get_text(" ", strip=True).lower()
        for tr in rows[:4]
        for c in tr.find_all(["th", "td"])
    )
    has_name = any(k in header_text for k in ("name of beneficial", "name and address",
                                              "beneficial owner", "5% holders",
                                              "directors and named", "name"))
    has_shares = any(k in header_text for k in ("shares beneficially", "number of shares",
                                                "shares owned", "amount and nature",
                                                "beneficially owned"))
    has_percent = "percent" in header_text or "%" in header_text
    return has_name and has_shares and has_percent


def find_ownership_table(soup):
    """Localiza a tabela de beneficial ownership após o cabeçalho da seção.

    Itera por todas as ocorrências do título (pode haver no TOC) e procura
    a próxima tabela cujo cabeçalho corresponda ao padrão Name/Shares/Percent.
    """
    headings = find_section_headings(soup)
    if not headings:
        return None, None

    for heading_el, heading_text in headings:
        nxt = heading_el
        for _ in range(200):  # busca mais ampla
            nxt = nxt.find_next("table")
            if nxt is None:
                break
            if looks_like_ownership_table(nxt):
                return nxt, heading_text
    return None, None


def clean_cell(text):
    """Limpa célula: remove footnote markers, normaliza espaços."""
    if text is None:
        return ""
    s = str(text).replace("\xa0", " ").replace("​", "")
    s = re.sub(r"\s+", " ", s).strip()
    # Remove sufixos de footnote tipo "(1)", "(2)(3)" no FINAL
    s = re.sub(r"\s*\((\d+)(?:\)\(\d+)*\)\s*$", "", s)
    return s


def parse_table(table):
    """Converte <table> BS4 em DataFrame, lidando com rowspan/colspan."""
    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for cell in tr.find_all(["td", "th"]):
            cells.append(clean_cell(cell.get_text(" ", strip=True)))
        if any(c for c in cells):
            rows.append(cells)
    if not rows:
        return pd.DataFrame()

    # Normaliza número de colunas (preenche com vazio à direita)
    max_cols = max(len(r) for r in rows)
    rows = [r + [""] * (max_cols - len(r)) for r in rows]

    # Identifica linha de cabeçalho: a primeira linha que contém alguma palavra-chave
    header_idx = 0
    for i, r in enumerate(rows[:5]):
        joined = " ".join(r).lower()
        if any(k in joined for k in ("name", "shares", "percent", "owner")):
            header_idx = i
            break

    header = rows[header_idx]
    # Remove colunas inteiramente vazias no cabeçalho (mas só se também forem vazias em todas as linhas)
    keep_cols = [i for i, h in enumerate(header) if h or any(r[i] for r in rows[header_idx + 1:])]
    header = [header[i] for i in keep_cols]
    data = [[r[i] for i in keep_cols] for r in rows[header_idx + 1:]]

    # Deduplica cabeçalhos vazios/duplicados
    seen = {}
    final_header = []
    for h in header:
        h = h or "col"
        if h in seen:
            seen[h] += 1
            final_header.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 0
            final_header.append(h)

    df = pd.DataFrame(data, columns=final_header)
    # Remove linhas totalmente vazias
    df = df[(df != "").any(axis=1)].reset_index(drop=True)
    return df


def parse_number(s):
    """Converte string tipo '12,345' ou '1.5%' em float; retorna None se não der."""
    if not s:
        return None
    s = str(s).strip().replace(",", "").replace("%", "").replace("$", "")
    if s in ("", "-", "—", "*"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def enrich_numeric_columns(df):
    """Adiciona versões numéricas das colunas que parecem numéricas."""
    for col in df.columns:
        sample = df[col].astype(str).head(10).str.replace(",", "").str.replace("%", "")
        numeric_like = sample.str.match(r"^-?\d+(\.\d+)?$").sum()
        if numeric_like >= 3:
            df[f"{col}__num"] = df[col].apply(parse_number)
    return df


def _derive_csv_name(url):
    """Deriva 'beneficial_ownership_<doc>.csv' a partir do nome do arquivo na URL."""
    last = url.rstrip("/").split("/")[-1]
    stem = re.sub(r"\.(htm|html|xml)$", "", last, flags=re.IGNORECASE) or "doc"
    return f"beneficial_ownership_{stem}.csv"


def scrape_beneficial_ownership(url=DEFAULT_URL, output_csv=None):
    print(f"Buscando: {url}")
    html = fetch_html(url)
    soup = BeautifulSoup(html, "lxml")
    print(f"  HTML: {len(html):,} chars | {len(soup.find_all('table'))} tabelas no doc")

    table, heading = find_ownership_table(soup)
    if table is None:
        print("Tabela 'Security Ownership' não encontrada.")
        return pd.DataFrame()

    print(f"  Header da seção: {heading[:120]}")
    df = parse_table(table)
    df = enrich_numeric_columns(df)
    print(f"  Linhas extraídas: {len(df)} | Colunas: {list(df.columns)}")

    if output_csv is None:
        output_csv = _derive_csv_name(url)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    print(f"\nSalvo em: {output_csv}")
    return df


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    output = sys.argv[2] if len(sys.argv) > 2 else None
    scrape_beneficial_ownership(url, output)

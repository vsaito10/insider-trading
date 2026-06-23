"""Scraping genérico de Form 3/4/5 (insider trading) do SEC EDGAR.

Funciona para qualquer CIK — seja de um insider específico (reportingOwner)
ou de uma empresa emissora (issuer, que retorna filings de todos os insiders).
"""
import requests
import xml.etree.ElementTree as ET
import pandas as pd
import time

HEADERS = {
    "User-Agent": "insider-trading-research vitorsaito95@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

TRANSACTION_CODES = {
    "P": "Compra (mercado aberto)",
    "S": "Venda (mercado aberto)",
    "A": "Concessão/Aquisição (grant)",
    "D": "Devolução ao emissor",
    "F": "Pagamento de imposto via ações",
    "M": "Exercício de opção (derivativo)",
    "G": "Doação",
    "X": "Exercício de opção in-the-money",
    "V": "Transação voluntária",
    "J": "Outra (ver footnote)",
    "C": "Conversão de derivativo",
    "I": "Discricionária",
    "W": "Sucessão/herança",
}


def normalize_cik(cik):
    """Aceita CIK como int ou string e devolve string de 10 dígitos."""
    return str(cik).strip().replace("CIK", "").zfill(10)


def get_filings(cik, forms=("4", "4/A"), date_start=None, date_end=None,
                include_history=True, verbose=True):
    """Lista filings de um CIK no EDGAR.

    Args:
        cik: CIK do insider (reportingOwner) ou da empresa (issuer).
        forms: Tipos de filing aceitos. Ex: ("4","4/A"), ("3","4","5","3/A","4/A","5/A").
        date_start, date_end: Período em "YYYY-MM-DD" (inclusivo). None = sem limite.
        include_history: Se True, busca também filings antigos em arquivos paginados.
        verbose: Imprime progresso.

    Returns:
        Lista de dicts: {accession, filing_date, primary_doc, form}.
    """
    cik = normalize_cik(cik)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    filings = []

    def _extract(block):
        for i, form in enumerate(block["form"]):
            if form not in forms:
                continue
            date = block["filingDate"][i]
            if date_start and date < date_start:
                continue
            if date_end and date > date_end:
                continue
            filings.append({
                "accession": block["accessionNumber"][i].replace("-", ""),
                "filing_date": date,
                "primary_doc": block["primaryDocument"][i],
                "form": form,
            })

    _extract(data["filings"]["recent"])

    if include_history:
        for f in data["filings"].get("files", []):
            try:
                r = requests.get(
                    f"https://data.sec.gov/submissions/{f['name']}",
                    headers=HEADERS, timeout=30,
                )
                r.raise_for_status()
                _extract(r.json())
                time.sleep(0.12)
            except Exception as e:
                if verbose:
                    print(f"  [WARN] Histórico {f['name']}: {e}")

    return filings


def fetch_xml(cik, accession, primary_doc):
    """Baixa o XML do filing. Tenta o CIK fornecido e cai para o CIK do filer."""
    cik_int = int(normalize_cik(cik))
    filer_cik = int(accession[:10])
    # primary_doc pode vir como "xslF345X05/wk-form4_xxx.xml" — pegamos só o arquivo
    xml_file = primary_doc.split("/")[-1]

    for c in {cik_int, filer_cik}:
        url = f"https://www.sec.gov/Archives/edgar/data/{c}/{accession}/{xml_file}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass

    raise ValueError(f"XML não acessível para o filing {accession}")


def _val(parent, tag):
    """Extrai texto de uma tag, lidando com o wrapper <value> do Form 4 XML."""
    el = parent.find(f".//{tag}")
    if el is None:
        return ""
    v = el.find("value")
    if v is not None and v.text:
        return v.text.strip()
    return el.text.strip() if el.text else ""


def _to_float(s):
    try:
        return float(s) if s else None
    except ValueError:
        return None


def parse_transactions(xml_text, filing_date, accession, form_type):
    """Extrai transações do XML Form 3/4/5."""
    root = ET.fromstring(xml_text)

    reporter = _val(root, "rptOwnerName")
    reporter_cik = _val(root, "rptOwnerCik")
    officer_title = _val(root, "officerTitle")
    is_officer = _val(root, "isOfficer") == "1"
    is_director = _val(root, "isDirector") == "1"
    is_ten_percent = _val(root, "isTenPercentOwner") == "1"
    issuer = _val(root, "issuerName")
    ticker = _val(root, "issuerTradingSymbol")

    rows = []
    for kind, tag in [("não-derivativo", "nonDerivativeTransaction"),
                      ("derivativo", "derivativeTransaction")]:
        for t in root.findall(f".//{tag}"):
            code = _val(t, "transactionCode")
            if not code:
                continue
            shares = _to_float(_val(t, "transactionShares"))
            price = _to_float(_val(t, "transactionPricePerShare"))
            shares_after = _to_float(_val(t, "sharesOwnedFollowingTransaction"))
            ad = _val(t, "transactionAcquiredDisposedCode")
            ownership = _val(t, "directOrIndirectOwnership")

            rows.append({
                "accession": accession,
                "form_type": form_type,
                "reporter": reporter,
                "reporter_cik": reporter_cik,
                "officer_title": officer_title,
                "is_officer": is_officer,
                "is_director": is_director,
                "is_ten_percent_owner": is_ten_percent,
                "issuer": issuer,
                "ticker": ticker,
                "filing_date": filing_date,
                "transaction_date": _val(t, "transactionDate") or filing_date,
                "security": _val(t, "securityTitle"),
                "kind": kind,
                "transaction_code": code,
                "transaction_type": TRANSACTION_CODES.get(code, code),
                "acquired_disposed": (
                    "Adquirido" if ad == "A" else ("Alienado" if ad == "D" else "")
                ),
                "shares": shares,
                "price_per_share_usd": price,
                "total_value_usd": round(shares * price, 2) if shares and price else None,
                "shares_after_transaction": shares_after,
                "ownership": (
                    "Direta" if ownership == "D" else ("Indireta" if ownership == "I" else "")
                ),
                "nature_of_ownership": _val(t, "natureOfOwnership"),
            })
    return rows


def scrape_cik(cik, date_start=None, date_end=None, forms=("4", "4/A"),
               include_history=True, reporter_name_filter=None, verbose=True):
    """Faz scraping completo das transações de um CIK.

    Args:
        cik: CIK do insider OU do emissor.
        date_start, date_end: Período "YYYY-MM-DD" inclusivo (None = sem limite).
        forms: Tipos aceitos (default: Form 4 e amendments).
        include_history: Inclui filings antigos (paginação).
        reporter_name_filter: Substring (case-insensitive) para filtrar pelo nome
            do reportingOwner. Útil quando o CIK é do emissor e você quer apenas
            um insider específico. Ex: "huang jen hsun".
        verbose: Imprime progresso.

    Returns:
        DataFrame com as transações.
    """
    cik = normalize_cik(cik)
    if verbose:
        print(f"\n=== CIK {cik} | forms={list(forms)} | {date_start or '...'} → {date_end or '...'} ===")

    filings = get_filings(cik, forms=forms, date_start=date_start,
                          date_end=date_end, include_history=include_history,
                          verbose=verbose)
    if verbose:
        print(f"Filings encontrados: {len(filings)}")

    rows = []
    for f in filings:
        try:
            xml_text = fetch_xml(cik, f["accession"], f["primary_doc"])
            if reporter_name_filter and reporter_name_filter.lower() not in xml_text.lower():
                time.sleep(0.1)
                continue
            txs = parse_transactions(xml_text, f["filing_date"], f["accession"], f["form"])
            rows.extend(txs)
            if verbose:
                print(f"  [{f['filing_date']}] {f['form']}: {len(txs)} transações | {f['accession']}")
        except Exception as e:
            if verbose:
                print(f"  [ERRO] {f['accession']}: {e}")
        time.sleep(0.12)

    return pd.DataFrame(rows)


def scrape_many(ciks, company_name, **kwargs):
    """Scrape consolidado de vários CIKs e salva em CSV nomeado pela empresa.

    Args:
        ciks: Lista de CIKs (insiders e/ou emissores).
        company_name: Nome (ou ticker) usado no nome do CSV de saída.
        **kwargs: Mesmos parâmetros de scrape_cik (date_start, date_end, forms, etc.).

    Returns:
        DataFrame único consolidado.
    """
    dfs = [scrape_cik(c, **kwargs) for c in ciks]
    dfs = [d for d in dfs if not d.empty]
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=["accession", "transaction_code", "transaction_date",
                                    "shares", "price_per_share_usd"])
    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    df = df.sort_values("transaction_date", ascending=False).reset_index(drop=True)

    output = f"insider_transactions_{company_name}.csv"
    df.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"\nSalvo em: {output}")
    return df


def summarize(df):
    """Imprime um resumo agregado e retorna o DataFrame de resumo."""
    if df.empty:
        print("Nenhuma transação.")
        return df
    print(f"\nTotal de transações: {len(df)}")
    summary = df.groupby(["reporter", "transaction_type"], dropna=False).agg(
        qtd_operacoes=("shares", "count"),
        total_acoes=("shares", "sum"),
        valor_total_usd=("total_value_usd", "sum"),
    ).reset_index()
    print("\n--- RESUMO POR INSIDER / TIPO ---")
    print(summary.to_string(index=False))
    return summary


def main():
    # Alta cúpula Nvidia - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001197649", # Jen-Hsun Huang
    #         "0001588670", # Colette M. Kress
    #         "0001347842", # Ajay K. Puri
    #         "0001283854", # Debora Shoquist
    #         "0001696841", # Timothy S. Teter
    #         ],
    #     company_name="nvda",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula AMD - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001405109", # Lisa Su
    #         "0001768248", # Darren Grasby
    #         "0001985800", # Philip Guido
    #         "0001452385", # Jean Hu
    #         "0001622864", # Forrest Norrod
    #         "0001449649", # Mark Papermaster
    #         ],
    #     company_name="amd",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Broadcom - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001211588", # Hock E. Tan
    #         "0001608425", # Charlie Kawwas
    #         "0002103553", # Ram Velaga
    #         "0001627720", # Mark Brazeal
    #         "0001670725", # Kirsten Spears
    #         ],
    #     company_name="avgo",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula MRVL - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001381430", # Matt Murphy
    #         "0001975808", # Sandeep Bharathi
    #         "0002007961", # Mark Casper
    #         "0001676204", # Chris Koopmans
    #         "0001635800", # Willem Meintjes
    #         ],
    #     company_name="mrvl",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Intel - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001008463", # Lip-Bu Tan
    #         "0001773301", # Aparna Bawa
    #         "0002082075", # Naga Chandrasekaran
    #         "0002136381", # Alex Katouzian
    #         "0001330891", # David Zinsner
    #         ],
    #     company_name="intc",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Micron - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001242654", # Sanjay Mehrotra
    #         "0001632063", # April Arnzen
    #         "0001689498", # Manish Bhatia
    #         "0001201490", # Michael Cordano
    #         "0001568238", # Scott J. DeBoer
    #         "0001498287", # Mark Murphy
    #         "0001311079", # Sumit Sadana
    #         "0001593199", # Michael Ray
    #         ],
    #     company_name="mu",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Qualcomm - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001559665", # Cristiano Amon
    #         "0001610762", # Heather Ace
    #         "0001888491", # Ann Chaplin
    #         "0001786420", # Akash Palkhiwala
    #         "0001687791", # Alex Rogers
    #         "0002053888", # Baaziz Achour
    #         "0001309507", # Mark McLaughlin
    #         "0001829225", # Sylvia Acevedo
    #         "0001191534", # Mark Fields
    #         "0001324015", # Jeffrey W. Henderson
    #         "0002083092", # Jeremy Kolter
    #         "0001207622", # Ann M. Livermore
    #         "0001290697", # Jamie S. Miller
    #         "0001656326", # Marie Myers
    #         "0001082930", # Irene B. Rosenfeld
    #         "0001815920", # Jean-Pascal Tricoire
    #         ],
    #     company_name="qcom",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Cadence - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001591933", # Anirudh Devgan
    #         "0001847883", # Paul Cunningham 
    #         "0002031609", # Paul Scannell
    #         "0001672685", # Marc Taxay
    #         "0001751946", # Chin-Chi Teng
    #         "0001718165", # John Wall
    #         ],
    #     company_name="cdns",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Synopsys - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001822289", # Sassine Ghazi
    #         "0001865504", # Shelagh Glaser
    #         "0001615442", # Mike Ellow
    #         "0001711671", # Janet Lee
    #         "0001249802", # Aart de Geus
    #         "0001652437", # Sujit Kankanwadi
    #         "0001206990", # John F. Jr. Runkel 
    #         ],
    #     company_name="snps",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula TSMC - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0002113717", # Che-Chia Wei
    #         "0002113729", # Yuh-Jier Mii
    #         "0002113680", # Yung-Chin Hou
    #         "0002115263", # Kevin Zhang
    #         "0002113698", # Shu-Hua Fang
    #         "0002113699", # Jen-Chau Huang
    #         "0002118010", # Ying-Lang Wang
    #         "0002113710", # Tzonz-Sheng Chang
    #         "0002113784", # Shien-Yang Wu
    #         "0002113762", # Choh Fei Yeap
    #         "0002113739", # Chun-Hsien Lee 
    #         "0002113031", # Min Cao
    #         "0002113705", # Yung-Haw Liaw
    #         "0002113688", # Syun-Ming Jang
    #         "0002113775", # Chue-San Yoo 
    #         "0002113771", # Jun He
    #         "0002113756", # Chris Horng-Dar Lin
    #         "0002114030", # Tzu-Sou Chuang
    #         "0002114032", # Lee-Chung Lu
    #         "0002114031", # Kuo-Chin Hsu
    #         "0002114029", # Juiping Chuang 
    #         "0002114027", # Pei-Hung Chen 
    #         "0002114064", # Yuan-Ko Hwang
    #         "0002114023", # Bor-Zen Tien 
    #         "0002114025", # Shyue-Shyh Lin 
    #         "0002114024", # Lipen Yuan
    #         ],
    #     company_name="tsm",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Applied Materials - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001183258", # Gary E. Dickerson
    #         "0001721738", # Prabu Raja
    #         "0001945425", # Tim Deane
    #         "0001809022", # Brice Hill
    #         "0001656471", # Teri Little
    #         "0001510815", # Omkaram Nalamasu
    #         ],
    #     company_name="amat",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Lam Research - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001343600", # Tim Archer
    #         "0001469469", # Doug Bettinger
    #         "0001966598", # Neil Fernandes
    #         "0001642415", # Ava Harter
    #         "0001707826", # Vahid Vahedi
    #         "0001707827", # Sesha Varadarajan
    #         ],
    #     company_name="lrcx",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula KLA Corporation - busca por CIK do insider
    df = scrape_many(
        ciks=[
            "0001196898", # Rick Wallace
            "0001582049", # Bren Higgins
            "0001642452", # Ahmad Khan
            "0001697041", # Brian Lorig
            "0001691769", # MaryBeth Wilkinson
            ],
        company_name="klac",
        date_start="2025-01-01",
        date_end="2026-12-31",
        forms=("4", "4/A"),
    )

    # # Alta cúpula Netflix - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001543133", # Spencer Neumann
    #         "0001583109", # Greg Peters
    #         "0001393838", # Ted Sarandos
    #         "0002065325", # Clete Willems
    #         "0001507747", # David Hyman
    #         ],
    #     company_name="nflx",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Cloudflare - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001786925", # Matthew Prince
    #         "0001786951", # Michelle Zatlyn
    #         "0001473289", # Thomas Seifert
    #         "0002128025", # Alissa Starzak
    #         ],
    #     company_name="net",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula ServiceNow - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001334944", # Bill McDermott
    #         "0001649609", # Jacqui Canney
    #         "0001197952", # Russ Elmer
    #         "0001667422", # Paul Fipps
    #         "0001465391", # Gina Mastantuono
    #         "0002103469", # Hossein Nowbar
    #         "0001891538", # Nick Tzitzon
    #         "0001781064", # Amit Zavery​
    #         ],
    #     company_name="now",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Adobe - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001224154", # Shantanu Narayen
    #         "0001997095", # Lara Balazs
    #         "0001584805", # Anil Chakravarthy
    #         "0001795424", # Gloria Chen
    #         "0001610062", # Dan Durn
    #         "0001643724", # Louise Pentland
    #         "0001494665", # David Wadhwani
    #         ],
    #     company_name="adbe",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Datadog - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001783990", # Olivier Pomel
    #         "0001783984", # Alexis Lê-Quôc
    #         "0001120741", # David Obstler
    #         "0001861590", # Adam Blitzer
    #         "0001783061", # Yanbing Li
    #         "0001757760", # David Galloreese
    #         "0001903980", # Sean Walters
    #         "0001903975", # Kerry Acocella
    #         ],
    #     company_name="ddog",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula MP Materials - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001831746", # James Litinsky
    #         "0001832050", # Michael Rosenthal
    #         "0001831686", # Ryan Corbett
    #         "0001862894", # Elliot Hoops
    #         "0001031609", # Randall Weisenburger
    #         "0001180940", # Connie Duckworth
    #         "0002059140", # David Gregory Infuso
    #         ],
    #     company_name="mp",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Constellation Energy - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001495634", # Joseph Dominguez
    #         "0001909010", # Kathleen Barrón
    #         "0001908991", # David O. Dardis
    #         "0001908980", # Daniel L. Eggers
    #         "0001824653", # Bryan Hanson
    #         "0001910426", # Mike Koehler
    #         "0001897905", # James McHugh
    #         "0001697866", # Andrew Novotny
    #         "0002103757", # Shane Smith
    #         ],
    #     company_name="ceg",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Vistra - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001268406", # Jim Burke
    #         "0001690128", # Stacey Doré
    #         "0001728553", # Scott A. Hudson
    #         "0001940494", # Kris Moldovan
    #         "0001703634", # Stephanie Zapata Moore
    #         "0001711936", # Scott Helm
    #         "0001558582", # Hilary E. Ackermann
    #         "0001629039", # Arcilia Acosta
    #         "0001605616", # Gavin Baiera
    #         "0001176453", # Paul M. Barbas
    #         "0001564675", # Lisa Crutchfield
    #         "0001712567", # Julie Lagacy
    #         "0002015795", # John Pitesa
    #         "0002015795", # John Sult
    #         "0002051447", # Rob Walters
    #         ],
    #     company_name="vst",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Talen - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0002028822", # Brad Berryman
    #         "0002102527", # Edward Casulli
    #         "0002135080", # Dan Kelly
    #         "0002028467", # Dale Lebsack
    #         "0001720720", # Mark McFarland
    #         "0002028530", # Chris Morice
    #         "0002028529", # Cole Muller
    #         "0001707031", # Terry Nutt
    #         "0001408305", # Darren Olagues
    #         "0002028528", # Anthony Plagens
    #         "0002028524", # Andy Wright
    #         ],
    #     company_name="tln",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Alta cúpula Oklo - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0002021433", # Jacob Dewitte
    #         "0002021786", # Caroline Cochran
    #         "0001855623", # Richard Craig Bealmear 
    #         "0002080717", # William Goodwin
    #         ],
    #     company_name="oklo",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # # Alta cúpula NRG - busca por CIK do insider
    # df = scrape_many(
    #     ciks=[
    #         "0001505252", # Robert J. Gaudette
    #         "0002080459", # Brad Bentley
    #         "0001940921", # Bruce Chung
    #         "0001734640", # Brian Curci
    #         "0002048902", # Gin Kirkland Kinney
    #         "0002049789", # Dak Liyanearachchi
    #         ],
    #     company_name="nrg",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     forms=("4", "4/A"),
    # )

    # Exemplo 2 (comente acima e descomente abaixo): TODOS os insiders da NVIDIA
    # df = scrape_many(
    #     ciks=["0001045810"],  # CIK do emissor (NVIDIA)
    #     company_name="nvda",
    #     date_start="2026-01-01",
    #     date_end="2026-12-31",
    # )

    # Exemplo 3: filtrar um insider específico dentro do emissor
    # df = scrape_many(
    #     ciks=["0001045810"],
    #     company_name="nvda_huang",
    #     date_start="2025-01-01",
    #     date_end="2026-12-31",
    #     reporter_name_filter="huang jen hsun",
    # )

    if df.empty:
        print("\nNenhuma transação encontrada.")
        return

    summarize(df)


if __name__ == "__main__":
    main()

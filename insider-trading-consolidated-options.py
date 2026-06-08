"""
Consolida exercícios de opção (Form 4 SEC, código M) por insider.

Uso:
    python insider-trading-consolidated-options.py <input.csv> [output.xlsx]

Lê o CSV gerado pelo scraper de Form 4 e produz um xlsx com:
- Aba Consolidado:  uma linha por insider com totais de exercícios de opção
- Aba Por security: breakdown reporter x securityTitle
- Aba Exercícios:   dump bruto apenas das transações M (derivativo + não-derivativo)
"""

from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

# Código SEC Form 4 para exercício de opção
EXERCICIO_OPCAO = "M"

COLUNAS_OBRIGATORIAS = [
    "reporter", "officer_title", "transaction_code", "kind",
    "shares", "price_per_share_usd", "total_value_usd",
    "transaction_date", "security", "acquired_disposed",
]


def validar(df: pd.DataFrame) -> None:
    faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
    if faltando:
        raise ValueError(f"Colunas obrigatórias ausentes: {faltando}")


def filtrar_exercicios(df: pd.DataFrame) -> pd.DataFrame:
    """Mantém apenas linhas com transaction_code == 'M'."""
    return df[df["transaction_code"] == EXERCICIO_OPCAO].copy()


def consolidar(df: pd.DataFrame) -> pd.DataFrame:
    """Agrupa por insider e calcula totais de exercício de opção.

    Em um Form 4 com código M, a SEC pode reportar até duas linhas:
      - não-derivativo (A/Adquirido): ações ordinárias recebidas — sempre presente
      - derivativo (D/Alienado): opções consumidas ao strike — nem sempre presente
        (em settlements de RSU, por ex., só a perna não-derivativa é registrada)
    Usamos a perna não-derivativa como fonte primária de ações recebidas e a
    derivativa, quando existe, para extrair o custo de strike.
    """
    linhas = []
    for reporter, g in df.groupby("reporter"):
        cargo = (
            g.sort_values("transaction_date", ascending=False)["officer_title"]
            .dropna()
            .iloc[0]
            if g["officer_title"].notna().any()
            else ""
        )

        deriv = g[g["kind"] == "derivativo"]
        nao_deriv = g[g["kind"] == "não-derivativo"]

        acoes_recebidas = nao_deriv["shares"].fillna(0).sum()
        opcoes_consumidas = deriv["shares"].fillna(0).sum()
        custo_strike_usd = deriv["total_value_usd"].fillna(0).sum()

        # preço médio de exercício (strike) ponderado pelas ações
        if opcoes_consumidas > 0 and custo_strike_usd > 0:
            preco_medio_strike = custo_strike_usd / opcoes_consumidas
        else:
            preco_medio_strike = 0.0

        # número de eventos: pega o maior lado para não subcontar
        n_eventos = max(len(deriv), len(nao_deriv))

        linhas.append({
            "reporter": reporter,
            "officer_title": cargo,
            "n_exercicios": n_eventos,
            "acoes_recebidas": acoes_recebidas,
            "opcoes_consumidas": opcoes_consumidas,
            "preco_medio_strike_usd": round(preco_medio_strike, 4),
            "custo_total_strike_usd": custo_strike_usd,
            "primeira_data": g["transaction_date"].min(),
            "ultima_data": g["transaction_date"].max(),
        })

    out = pd.DataFrame(linhas).sort_values(
        "acoes_recebidas", ascending=False
    )
    return out.reset_index(drop=True)


def pivot_por_security(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot reporter x securityTitle com soma de ações exercidas."""
    if df.empty:
        return pd.DataFrame()
    p = df.pivot_table(
        index="reporter",
        columns="security",
        values="shares",
        aggfunc="sum",
        fill_value=0,
    )
    p["TOTAL_ACOES"] = p.sum(axis=1)
    return p.sort_values("TOTAL_ACOES", ascending=False)


def exportar_xlsx(
    consolidado: pd.DataFrame,
    pivot: pd.DataFrame,
    raw: pd.DataFrame,
    destino: Path,
) -> None:
    """Grava xlsx com formatação financeira."""
    with pd.ExcelWriter(destino, engine="openpyxl") as writer:
        consolidado.to_excel(writer, sheet_name="Consolidado", index=False)
        if not pivot.empty:
            pivot.to_excel(writer, sheet_name="Por security")
        raw.to_excel(writer, sheet_name="Exercícios", index=False)

        _wb = writer.book
        ws = writer.sheets["Consolidado"]
        for col_idx, col_name in enumerate(consolidado.columns, start=1):
            nome = col_name.lower()
            if "usd" in nome:
                letra = ws.cell(row=1, column=col_idx).column_letter
                for cell in ws[letra][1:]:
                    cell.number_format = '"$"#,##0.00'
            elif "acoes" in nome or "exercidas" in nome or nome == "n_exercicios":
                letra = ws.cell(row=1, column=col_idx).column_letter
                for cell in ws[letra][1:]:
                    cell.number_format = "#,##0"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    src = Path(sys.argv[1])
    dst = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else src.with_name(f"{src.stem}_exercicios_opcao.xlsx")
    )

    df = pd.read_csv(src)
    validar(df)

    exercicios = filtrar_exercicios(df)
    if exercicios.empty:
        print(f"\n[!] Nenhuma transacao de codigo '{EXERCICIO_OPCAO}' "
              f"(Exercicio de opcao) encontrada em {src}")
        return 0

    consolidado = consolidar(exercicios)
    pivot = pivot_por_security(exercicios)
    exportar_xlsx(consolidado, pivot, exercicios, dst)

    print(f"\n[OK] {len(exercicios)} linhas de exercicio consolidadas em "
          f"{len(consolidado)} insiders")
    print(f"[OK] Arquivo: {dst}\n")
    print(consolidado[[
        "reporter", "n_exercicios", "acoes_recebidas", "opcoes_consumidas",
        "preco_medio_strike_usd", "custo_total_strike_usd",
    ]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

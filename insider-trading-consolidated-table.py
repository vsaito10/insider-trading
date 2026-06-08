"""
Consolida transações de insiders (Form 4 SEC) por pessoa.

Uso:
    python consolidate.py <input.csv> [output.xlsx]

Lê o CSV gerado pelo scraper de Form 4 e produz um xlsx com:
- Aba Consolidado: uma linha por insider com compras/vendas reais e movimentação contábil
- Aba Por código:  pivot reporter x transaction_code
- Aba Transações:  dump bruto para auditoria
"""

from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

# Códigos SEC Form 4 — separar sinal de mercado de movimentação contábil
COMPRA_MERCADO = {"P"}
VENDA_MERCADO = {"S"}
GRANTS = {"A", "M"}           # aquisição não-monetária (RSU, exercício de opção)
RETENCAO_IMPOSTO = {"F"}      # ações retidas para pagar imposto
DOACAO = {"G"}                # transferência, não é mercado

COLUNAS_OBRIGATORIAS = [
    "reporter", "officer_title", "transaction_code",
    "shares", "total_value_usd", "transaction_date",
]


def validar(df: pd.DataFrame) -> None:
    faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
    if faltando:
        raise ValueError(f"Colunas obrigatórias ausentes: {faltando}")


def soma_usd(df: pd.DataFrame, codigos: set[str]) -> float:
    mask = df["transaction_code"].isin(codigos)
    return df.loc[mask, "total_value_usd"].fillna(0).sum()


def soma_shares(df: pd.DataFrame, codigos: set[str]) -> float:
    mask = df["transaction_code"].isin(codigos)
    return df.loc[mask, "shares"].fillna(0).sum()


def consolidar(df: pd.DataFrame) -> pd.DataFrame:
    """Agrupa por insider e calcula compras/vendas reais e movimentos contábeis."""
    linhas = []
    for reporter, g in df.groupby("reporter"):
        # cargo mais recente
        cargo = (
            g.sort_values("transaction_date", ascending=False)["officer_title"]
            .dropna()
            .iloc[0]
            if g["officer_title"].notna().any()
            else ""
        )
        compras_usd = soma_usd(g, COMPRA_MERCADO)
        compras_acoes = soma_shares(g, COMPRA_MERCADO)
        vendas_usd = soma_usd(g, VENDA_MERCADO)
        vendas_acoes = soma_shares(g, VENDA_MERCADO)

        linhas.append({
            "reporter": reporter,
            "officer_title": cargo,
            "compras_mercado_usd": compras_usd,
            "compras_mercado_acoes": compras_acoes,
            "vendas_mercado_usd": vendas_usd,
            "vendas_mercado_acoes": vendas_acoes,
            "net_mercado_usd": compras_usd - vendas_usd,
            "grants_acoes": soma_shares(g, GRANTS),
            "retencao_imposto_acoes": soma_shares(g, RETENCAO_IMPOSTO),
            "doacoes_acoes_alienado": soma_shares(
                g[g.get("acquired_disposed") == "Alienado"], DOACAO
            ) if "acquired_disposed" in g.columns else soma_shares(g, DOACAO),
            "n_transacoes": len(g),
            "primeira_transacao": g["transaction_date"].min(),
            "ultima_transacao": g["transaction_date"].max(),
        })

    out = pd.DataFrame(linhas).sort_values("vendas_mercado_usd", ascending=False)
    return out.reset_index(drop=True)


def pivot_por_codigo(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot reporter x transaction_code com soma em USD."""
    p = df.pivot_table(
        index="reporter",
        columns="transaction_code",
        values="total_value_usd",
        aggfunc="sum",
        fill_value=0,
    )
    p["TOTAL_USD"] = p.sum(axis=1)
    return p.sort_values("TOTAL_USD", ascending=False)


def exportar_xlsx(
    consolidado: pd.DataFrame,
    pivot: pd.DataFrame,
    raw: pd.DataFrame,
    destino: Path,
) -> None:
    """Grava xlsx com formatação financeira."""
    with pd.ExcelWriter(destino, engine="openpyxl") as writer:
        consolidado.to_excel(writer, sheet_name="Consolidado", index=False)
        pivot.to_excel(writer, sheet_name="Por código")
        raw.to_excel(writer, sheet_name="Transações", index=False)

        # formatação básica (moeda nas colunas USD)
        _wb = writer.book
        ws = writer.sheets["Consolidado"]
        for col_idx, col_name in enumerate(consolidado.columns, start=1):
            if "usd" in col_name.lower():
                letra = ws.cell(row=1, column=col_idx).column_letter
                for cell in ws[letra][1:]:
                    cell.number_format = '"$"#,##0.00'
            elif "acoes" in col_name.lower() or col_name == "n_transacoes":
                letra = ws.cell(row=1, column=col_idx).column_letter
                for cell in ws[letra][1:]:
                    cell.number_format = "#,##0"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(f"{src.stem}_consolidated.xlsx")

    df = pd.read_csv(src)
    validar(df)

    consolidado = consolidar(df)
    pivot = pivot_por_codigo(df)
    exportar_xlsx(consolidado, pivot, df, dst)

    # resumo no terminal
    print(f"\n✓ {len(df)} transações consolidadas em {len(consolidado)} insiders")
    print(f"✓ Arquivo: {dst}\n")
    print(consolidado[[
        "reporter", "compras_mercado_usd", "vendas_mercado_usd", "net_mercado_usd"
    ]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
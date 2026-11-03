SECTOR_BY_SYMBOL = {
    "F": "Consumer Discretionary",
    "CCL": "Consumer Discretionary",
    "RIVN": "Consumer Discretionary",
    "BAC": "Financials",
    "SOFI": "Financials",
    "NU": "Financials",
    "SNAP": "Communication Services",
    "PLTR": "Information Technology",
    "INTC": "Information Technology",
    "PFE": "Health Care",
}

SECTOR_ETF_BY_SECTOR = {
    "Consumer Discretionary": "XLY",
    "Financials": "XLF",
    "Communication Services": "XLC",
    "Information Technology": "XLK",
    "Health Care": "XLV",
}

UNCLASSIFIED_SECTOR = "Unclassified"


def sector_for_symbol(symbol: str) -> str:
    return SECTOR_BY_SYMBOL.get(symbol, UNCLASSIFIED_SECTOR)

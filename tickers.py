"""
Known ticker list for extraction.
Combines meme stocks, popular retail favorites, and common S&P 500 names.
We also maintain a stopword list to filter common English words that look like tickers.
"""

# Words that look like tickers but aren't — prevents false positives
STOPWORDS = {
    "A", "I", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF",
    "IN", "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP",
    "US", "WE", "ALL", "AND", "ARE", "BUT", "CAN", "DID", "FOR", "GET",
    "GOT", "HAD", "HAS", "HIM", "HIS", "HOW", "ITS", "LET", "NEW", "NOT",
    "NOW", "OFF", "OLD", "ONE", "OUR", "OUT", "OWN", "PUT", "SAY", "SEE",
    "SHE", "THE", "TOO", "TWO", "USE", "WAS", "WAY", "WHO", "WHY", "WIN",
    "WITH", "YOU", "YOUR", "THEY", "THAT", "THIS", "BEEN", "WERE",
    "HAVE", "FROM", "WILL", "WHEN", "WHAT", "THEN", "THAN", "SAID", "ALSO",
    "EACH", "LIKE", "LONG", "MAKE", "MANY", "MORE", "MOST", "MUCH", "OVER",
    "SAME", "SOME", "SUCH", "TAKE", "THEM", "WELL", "WENT", "JUST", "ONLY",
    "EVEN", "BACK", "GOOD", "INTO", "LOOK", "COME", "DOES", "CALL", "GIVE",
    "KNOW", "NEED", "NEXT", "PART", "PLAY", "REAL", "SEEM", "SOON", "STOP",
    "TELL", "VERY", "WANT", "WORK", "YEAR", "USED", "DAYS", "WEEK", "TIME",
    "HIGH", "DOWN", "OPEN", "BEAR", "BULL", "LOSS", "GAIN", "RISK", "HOLD",
    "SOLD", "SELL", "CASH", "FUND", "RATE", "BANK", "PLAN", "WALL", "MOON",
    "APES", "YOLO", "FOMO", "HODL", "ROFL", "LMAO", "IIRC", "IMHO", "FYI",
    "IMO", "TBH", "LOL", "BRO", "GUY", "EPS", "ATH", "DD", "OG", "DFV",
    "WSB", "RH", "EDIT", "TLDR", "ETF", "IPO", "CEO", "CFO", "CTO", "SEC",
    "FED", "GDP", "CPI", "EOD", "EOW", "ATM", "OTM", "ITM", "PUT", "CALL",
    "PUTS", "CALLS", "FWIW", "AFAIK", "EOM", "EOY", "QOQ", "YOY",
    "USA", "USD", "EUR", "GBP", "JPY", "NEWS", "POST", "TYPE", "LINK",
    "SAID", "SAYS", "SHOW", "STAY", "FEEL", "PAID", "BOTH", "HELP", "HARD",
    "EASY", "FREE", "FULL", "HUGE", "IDEA", "LAST", "LEFT", "LESS", "LIVE",
    "LOVE", "MOVE", "NICE", "ONCE", "PAST", "POOR", "RATE", "READ", "RICH",
    "RISE", "ROLE", "RULE", "SAFE", "SIDE", "SIZE", "SLOW", "SORT", "SURE",
    "TERM", "TURN", "TYPE", "VIEW", "WAIT", "WIDE", "WISE", "WENT", "WORD",
}

# Curated list of notable tickers — these are always checked even without regex match
KNOWN_TICKERS = {
    # Meme stocks / retail favorites
    "GME", "AMC", "BBBY", "KOSS", "BB", "NOK", "EXPR", "CLOV", "WISH",
    "WKHS", "RIDE", "SPCE", "SNDL", "TLRY", "ACB", "CGC", "APHA",
    "PLTR", "CCIV", "LCID", "RIVN", "NKLA", "HYLN", "GOEV", "OPEN",
    "UWMC", "RKT", "SKLZ", "DKNG", "PENN", "FUBO", "MVIS", "NNDM",
    "OCGN", "VXRT", "BNGO", "ZNGA", "PRPL", "HIMS", "BARK", "BODY",
    "FFIE", "MULN", "ATER", "HCMC", "TRCH", "MMAT", "IDEX", "CTRM",
    "NAKD", "MINE", "XELA", "WISA", "PROG", "BBIG", "ESSC", "DWAC",
    "PHUN", "TTOO", "EEMD", "MEGL",

    # Mega cap / widely discussed
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "TSLA", "META", "NVDA",
    "NFLX", "BABA", "ORCL", "INTC", "AMD", "QCOM", "AVGO", "TXN",
    "ASML", "TSM", "SHOP", "SQ", "PYPL", "COIN", "HOOD", "SOFI",
    "AFRM", "UPST", "LMND", "CLOV", "RBLX", "U", "SNAP", "PINS",
    "SPOT", "ZM", "DOCU", "CRWD", "NET", "DDOG", "SNOW", "PATH",
    "AI", "PLTR", "SMAR", "TWLO", "OKTA", "ZS", "PANW", "FTNT",

    # Finance / banks
    "JPM", "GS", "MS", "BAC", "C", "WFC", "USB", "PNC", "TFC", "SCHW",
    "BLK", "BX", "KKR", "APO", "ARES",

    # Energy
    "XOM", "CVX", "COP", "MPC", "VLO", "PSX", "OXY", "DVN", "FANG",
    "PXD", "HAL", "SLB", "BKR",

    # Pharma / biotech
    "PFE", "MRNA", "BNTX", "JNJ", "LLY", "ABBV", "BMY", "GILD", "BIIB",
    "REGN", "VRTX", "ALNY", "SGEN", "IONS",

    # Retail
    "WMT", "TGT", "COST", "HD", "LOW", "AMZN", "ETSY", "EBAY", "W",

    # Crypto adjacent
    "MSTR", "HUT", "MARA", "RIOT", "CLSK", "BTBT", "CIFR",

    # ETFs sometimes discussed
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "ARKK", "SOXL", "TQQQ",
    "SPXL", "UVXY", "VIX",
}

# All tickers combined for fast lookup
ALL_TICKERS = KNOWN_TICKERS.copy()

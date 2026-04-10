"""List all crypto symbols available on our cTrader account."""
import json
from fx_pro_bot.trading.auth import TokenStore
from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.config.settings import Settings

s = Settings()
ts = TokenStore(s.ctrader_token_path)
tokens = ts.load()

client = CTraderClient(
    client_id=s.ctrader_client_id,
    client_secret=s.ctrader_client_secret,
    access_token=tokens.access_token,
    account_id=s.ctrader_account_id,
    host_type=s.ctrader_host_type,
    refresh_token=tokens.refresh_token,
)
client.start()

resp = client.get_symbols()

crypto_keywords = [
    "BITCOIN", "ETHEREUM", "SOLANA", "RIPPLE", "XRP",
    "DOGE", "DOGECOIN", "CARDANO", "ADA", "LITECOIN", "LTC",
    "CHAINLINK", "LINK", "POLKADOT", "DOT", "AVALANCHE", "AVAX",
    "BNB", "MATIC", "POLYGON", "SHIB", "PEPE", "UNI", "UNISWAP",
    "AAVE", "APT", "APTOS", "NEAR", "SUI", "ARB", "ARBITRUM",
    "OP", "OPTIMISM", "FIL", "FILECOIN", "ATOM", "COSMOS",
    "EOS", "TRON", "TRX", "STELLAR", "XLM", "ALGO", "ALGORAND",
    "SAND", "MANA", "AXS", "AXIE", "FTM", "FANTOM",
    "CRYPTO", "COIN",
]

details_map = {}
batch_size = 50
id_list = [sym.symbolId for sym in resp.symbol]
for i in range(0, len(id_list), batch_size):
    chunk = id_list[i : i + batch_size]
    try:
        det_resp = client.get_symbol_details(chunk)
        for d in det_resp.symbol:
            details_map[d.symbolId] = d
    except Exception as e:
        print(f"  batch {i} error: {e}")

results = []
for sym in resp.symbol:
    name = sym.symbolName if hasattr(sym, "symbolName") else ""
    upper = name.upper()
    if any(kw in upper for kw in crypto_keywords):
        det = details_map.get(sym.symbolId)
        enabled = getattr(sym, "enabled", True)
        digits = getattr(det, "digits", "?") if det else "?"
        lot_size = getattr(det, "lotSize", "?") if det else "?"
        min_vol = getattr(det, "minVolume", "?") if det else "?"
        step_vol = getattr(det, "stepVolume", "?") if det else "?"
        results.append({
            "id": sym.symbolId,
            "name": name,
            "enabled": enabled,
            "digits": digits,
            "lotSize": lot_size,
            "minVolume": min_vol,
            "stepVolume": step_vol,
        })

results.sort(key=lambda x: x["name"])
for r in results:
    status = "OK" if r["enabled"] else "DISABLED"
    print(
        f"  {r['name']:20s}  id={r['id']:5d}  digits={r['digits']}  "
        f"lot={r['lotSize']}  minVol={r['minVolume']}  step={r['stepVolume']}  [{status}]"
    )

print(f"\nВсего крипто: {len(results)} / {len(resp.symbol)} символов")
client.stop()

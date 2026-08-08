#!/usr/bin/env python3
"""
Memecoin Signal Bot - Solana / Phantom
========================================
Monitora:
  1) Il tuo wallet Phantom (saldo SOL, token posseduti, transazioni)
  2) Prezzo/volume dei token che possiedi -> segnali di VENDITA (take profit,
     stop loss, inversione di momentum, rottura trend)
  3) Una watchlist di memecoin che NON possiedi -> segnali di ACQUISTO
     (momentum, volume spike, trend rialzista)
  4) Wallet di terzi (tracked_wallets) -> alert quando comprano/vendono
  5) [OPZIONALE, disattivo di default] Trading automatico via Jupiter:
     se trading.enabled=true nel config, i segnali buy/sell vengono
     ESEGUITI DAVVERO come swap on-chain, non solo notificati.

Fonte dati prezzo: DexScreener API (pubblica, no API key).

IMPORTANTE: questo è uno strumento euristico basato su regole tecniche
configurabili. NON è consulenza finanziaria e NON garantisce risultati:
i memecoin sono estremamente volatili e manipolabili (pump & dump).
Con il trading automatico attivo, il bot spende fondi reali senza
conferma umana: usa solo un wallet dedicato con capitale che puoi
permetterti di perdere interamente.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone
from keep_alive import keep_alive

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

LAMPORTS_PER_SOL = 1_000_000_000
ALERT_COOLDOWN_SECONDS = 1800  # non ripetere lo stesso segnale sullo stesso token prima di 30 min

DEFAULT_CONFIG = {
    "wallet_address": "INSERISCI_QUI_IL_TUO_INDIRIZZO_PHANTOM",
    "discord_webhook_url": "INSERISCI_QUI_IL_TUO_WEBHOOK_DISCORD",
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "poll_interval_seconds": 60,

    "watchlist": {
        "ESEMPIO_MINT_ADDRESS_1": {"note": "sostituisci con mint reali da tenere d'occhio"}
    },

    "tracked_wallets": {
        "ESEMPIO_WALLET_DA_TRACCIARE": {"label": "es. wallet noto per aver fatto exit su memecoin"}
    },

    "positions": {
        "_comment": "Facoltativo: prezzo medio di carico per i token che possiedi, per TP/SL precisi. Se assente, il bot usa il primo prezzo osservato come riferimento.",
        "ESEMPIO_MINT_ADDRESS_2": {"entry_price_usd": 0.0000123}
    },

    "signal_settings": {
        "take_profit_pct": 50,
        "stop_loss_pct": 20,
        "momentum_buy_pct_5m": 8,
        "momentum_sell_drop_pct_5m": -10,
        "volume_spike_multiplier": 3,
        "ma_short_periods": 5,
        "ma_long_periods": 15,
        "min_liquidity_usd": 5000
    },

    "wallet_alerts": {
        "min_sol_change_to_alert": 0.001,
        "alert_on_new_tokens": True,
        "alert_on_token_balance_change": True,
        "alert_on_new_transactions": True
    },

    "auto_discover": {
        "_comment": "Scan automatico dei token in tendenza su pump.fun, oltre alla watchlist statica. Rischio più alto: include token sconosciuti/appena lanciati. Usa comunque i filtri di signal_settings (liquidità minima, ecc.).",
        "enabled": False,
        "scan_limit": 50
    },

    "trading": {
        "_comment": "TRADING AUTOMATICO REALE. enabled=false di default per sicurezza. La chiave privata NON va mai messa qui: si imposta come variabile d'ambiente (vedi README).",
        "enabled": False,
        "private_key_env_var": "WALLET_PRIVATE_KEY",
        "max_usd_per_trade": 10,
        "max_usd_per_day": 30,
        "slippage_bps": 300,
        "priority_fee_lamports": 100000
    }
}


# ---------------------------------------------------------------------------
# Config / State
# ---------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Creato {CONFIG_PATH}. Compilalo con i tuoi dati (wallet, webhook, watchlist) e riavvia.")
        raise SystemExit(0)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "sol_balance": None,
        "tokens": {},
        "last_signature": None,
        "price_history": {},          # mint -> list of {ts, price, volume_h1}
        "last_alerts": {},            # mint -> {signal_type: ts}
        "reference_price": {},        # mint -> first observed price (fallback entry)
        "tracked_wallet_tokens": {},  # wallet -> {mint: amount}
        "tracked_wallet_last_sig": {} # wallet -> signature
    }


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Solana RPC (wallet)
# ---------------------------------------------------------------------------

def rpc_call(rpc_url, method, params):
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]


def get_sol_balance(rpc_url, wallet):
    result = rpc_call(rpc_url, "getBalance", [wallet])
    return result["value"] / LAMPORTS_PER_SOL


def get_token_balances(rpc_url, wallet):
    """Ritorna {mint: {'amount': float, 'decimals': int}} per i token SPL nel wallet."""
    result = rpc_call(
        rpc_url,
        "getTokenAccountsByOwner",
        [wallet, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}],
    )
    tokens = {}
    for account in result["value"]:
        info = account["account"]["data"]["parsed"]["info"]
        mint = info["mint"]
        amount = float(info["tokenAmount"]["uiAmountString"] or 0)
        decimals = int(info["tokenAmount"]["decimals"])
        if amount > 0:
            tokens[mint] = {"amount": amount, "decimals": decimals}
    return tokens


def get_recent_signatures(rpc_url, wallet, limit=10):
    return rpc_call(rpc_url, "getSignaturesForAddress", [wallet, {"limit": limit}])


# ---------------------------------------------------------------------------
# DexScreener (prezzi/volume memecoin)
# ---------------------------------------------------------------------------

def get_token_market_data(mint):
    """Ritorna dati del pair con più liquidità per un mint, o None se non trovato."""
    try:
        resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs") or []
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None
        best = max(solana_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        return {
            "symbol": best.get("baseToken", {}).get("symbol", mint[:6]),
            "name": best.get("baseToken", {}).get("name", mint),
            "price_usd": float(best.get("priceUsd", 0) or 0),
            "price_change_m5": float(best.get("priceChange", {}).get("m5", 0) or 0),
            "price_change_h1": float(best.get("priceChange", {}).get("h1", 0) or 0),
            "volume_h1": float(best.get("volume", {}).get("h1", 0) or 0),
            "liquidity_usd": float(best.get("liquidity", {}).get("usd", 0) or 0),
            "url": best.get("url", f"https://dexscreener.com/solana/{mint}"),
        }
    except Exception as e:
        print(f"[!] Errore DexScreener per {mint}: {e}")
        return None


def discover_trending_tokens(limit=50):
    """Scarica i token più attivi/di tendenza da pump.fun (API non ufficiale, best effort).
    Ritorna una lista di mint address. Se l'API non risponde, ritorna lista vuota
    (il bot continua a funzionare solo con la watchlist statica)."""
    try:
        resp = requests.get(
            "https://frontend-api.pump.fun/coins",
            params={"offset": 0, "limit": limit, "sort": "market_cap", "order": "DESC", "includeNsfw": "false"},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            print(f"[!] pump.fun ha risposto {resp.status_code}, salto lo scan automatico questo ciclo.")
            return []
        coins = resp.json()
        return [c["mint"] for c in coins if c.get("mint")]
    except Exception as e:
        print(f"[!] Errore scan pump.fun: {e}")
        return []


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord_alert(webhook_url, title, description, color=0x9945FF, fields=None):
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = fields
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        if resp.status_code >= 300:
            print(f"[!] Errore invio Discord: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[!] Errore invio Discord: {e}")


def can_alert(state, mint, signal_type):
    last = state.get("last_alerts", {}).get(mint, {}).get(signal_type)
    if last is None:
        return True
    return (time.time() - last) >= ALERT_COOLDOWN_SECONDS


def mark_alerted(state, mint, signal_type):
    state.setdefault("last_alerts", {}).setdefault(mint, {})[signal_type] = time.time()


# ---------------------------------------------------------------------------
# Analisi tecnica (momentum / volume / trend)
# ---------------------------------------------------------------------------

def update_price_history(state, mint, price, volume_h1, max_samples=30):
    history = state.setdefault("price_history", {}).setdefault(mint, [])
    history.append({"ts": time.time(), "price": price, "volume_h1": volume_h1})
    if len(history) > max_samples:
        del history[0: len(history) - max_samples]


def moving_average(history, n):
    if len(history) < n:
        return None
    return sum(h["price"] for h in history[-n:]) / n


def volume_spike_detected(history, multiplier):
    if len(history) < 5:
        return False
    recent = history[-1]["volume_h1"]
    baseline = sum(h["volume_h1"] for h in history[:-1]) / max(len(history) - 1, 1)
    if baseline <= 0:
        return False
    return recent >= baseline * multiplier


def evaluate_buy_signal(market, history, settings):
    reasons = []
    if market["liquidity_usd"] < settings["min_liquidity_usd"]:
        return False, ["liquidità troppo bassa, scartato per sicurezza"]

    if market["price_change_m5"] >= settings["momentum_buy_pct_5m"]:
        reasons.append(f"momentum: +{market['price_change_m5']:.1f}% negli ultimi 5m")

    if volume_spike_detected(history, settings["volume_spike_multiplier"]):
        reasons.append(f"volume spike rilevato (≥{settings['volume_spike_multiplier']}x la media)")

    ma_short = moving_average(history, settings["ma_short_periods"])
    ma_long = moving_average(history, settings["ma_long_periods"])
    if ma_short and ma_long and ma_short > ma_long:
        reasons.append("trend rialzista (MA corta > MA lunga)")

    # Richiede almeno 2 conferme su 3 per ridurre falsi positivi
    triggered = len(reasons) >= 2
    return triggered, reasons


def evaluate_sell_signal(market, history, entry_price, settings):
    reasons = []
    if entry_price and entry_price > 0:
        change_from_entry = ((market["price_usd"] - entry_price) / entry_price) * 100
        if change_from_entry >= settings["take_profit_pct"]:
            reasons.append(f"take profit: +{change_from_entry:.1f}% dal prezzo di riferimento")
        if change_from_entry <= -settings["stop_loss_pct"]:
            reasons.append(f"stop loss: {change_from_entry:.1f}% dal prezzo di riferimento")

    if market["price_change_m5"] <= settings["momentum_sell_drop_pct_5m"]:
        reasons.append(f"crollo rapido: {market['price_change_m5']:.1f}% negli ultimi 5m")

    ma_short = moving_average(history, settings["ma_short_periods"])
    ma_long = moving_average(history, settings["ma_long_periods"])
    if ma_short and ma_long and ma_short < ma_long:
        reasons.append("trend ribassista (MA corta < MA lunga)")

    triggered = len(reasons) >= 1  # per la vendita basta un segnale forte (es. stop loss) per essere prudenti
    return triggered, reasons


# ---------------------------------------------------------------------------
# Wallet check (saldo / token / tx)
# ---------------------------------------------------------------------------

def check_wallet(config, state):
    wallet = config["wallet_address"]
    rpc_url = config["rpc_url"]
    webhook = config["discord_webhook_url"]
    wa = config["wallet_alerts"]

    sol_balance = get_sol_balance(rpc_url, wallet)
    if state["sol_balance"] is not None:
        diff = sol_balance - state["sol_balance"]
        if abs(diff) >= wa["min_sol_change_to_alert"]:
            direction = "🟢 Ricevuto" if diff > 0 else "🔴 Inviato/Speso"
            send_discord_alert(
                webhook, f"{direction} SOL",
                f"Variazione: **{diff:+.4f} SOL** — Nuovo saldo: **{sol_balance:.4f} SOL**",
                color=0x14F195 if diff > 0 else 0xFF4C4C,
            )
    state["sol_balance"] = sol_balance

    tokens = get_token_balances(rpc_url, wallet)
    old_tokens = state.get("tokens", {})
    for mint, info in tokens.items():
        amount = info["amount"]
        old_amount = old_tokens.get(mint, {}).get("amount")
        if mint not in old_tokens and wa["alert_on_new_tokens"]:
            send_discord_alert(webhook, "🆕 Nuovo token nel wallet", f"Mint: {mint}\nQuantità: {amount}", color=0xFFD700)
        elif mint in old_tokens and amount != old_amount and wa["alert_on_token_balance_change"]:
            send_discord_alert(webhook, "🔄 Variazione quantità token",
                                f"Mint: {mint}\n{old_amount} → {amount}", color=0x00BFFF)
    for mint in old_tokens:
        if mint not in tokens:
            send_discord_alert(webhook, "🚪 Token azzerato/venduto", f"Mint: {mint}", color=0x808080)
    state["tokens"] = tokens

    if wa["alert_on_new_transactions"]:
        sigs = get_recent_signatures(rpc_url, wallet, limit=10)
        if sigs:
            newest_sig = sigs[0]["signature"]
            if state["last_signature"] and state["last_signature"] != newest_sig:
                new_sigs = []
                for s in sigs:
                    if s["signature"] == state["last_signature"]:
                        break
                    new_sigs.append(s)
                for s in reversed(new_sigs):
                    status = "✅ Successo" if not s.get("err") else "❌ Fallita"
                    send_discord_alert(webhook, "📝 Nuova transazione",
                                        f"Stato: {status}\nhttps://solscan.io/tx/{s['signature']}", color=0x5865F2)
            state["last_signature"] = newest_sig

    return tokens, state


# ---------------------------------------------------------------------------
# Tracking wallet esterni (copy monitoring - "wallet ricchi di memecoin")
# ---------------------------------------------------------------------------

def check_tracked_wallets(config, state):
    """Traccia wallet pubblici di terzi: alert quando comprano/vendono un token.
    Usa solo dati pubblici on-chain, nessuna autorizzazione richiesta né possibile."""
    rpc_url = config["rpc_url"]
    webhook = config["discord_webhook_url"]
    tracked = config.get("tracked_wallets", {})

    for wallet, meta in tracked.items():
        if wallet.startswith("_") or wallet == "ESEMPIO_WALLET_DA_TRACCIARE":
            continue
        label = meta.get("label", wallet[:6])

        try:
            tokens = get_token_balances(rpc_url, wallet)
        except Exception as e:
            print(f"[!] Errore leggendo wallet tracciato {wallet}: {e}")
            continue

        old_tokens = state.setdefault("tracked_wallet_tokens", {}).get(wallet, {})

        for mint, info in tokens.items():
            amount = info["amount"]
            old_amount = old_tokens.get(mint, {}).get("amount")
            if mint not in old_tokens:
                market = get_token_market_data(mint)
                symbol = market["symbol"] if market else mint[:6]
                if can_alert(state, f"{wallet}:{mint}", "wallet_buy"):
                    send_discord_alert(
                        webhook,
                        f"🐋 {label} ha comprato: {symbol}",
                        f"Quantità: {amount}\nMint: {mint}" +
                        (f"\nPrezzo attuale: ${market['price_usd']:.8f}" if market else ""),
                        color=0x14F195,
                        fields=[{"name": "Wallet tracciato", "value": f"https://solscan.io/account/{wallet}", "inline": False}],
                    )
                    mark_alerted(state, f"{wallet}:{mint}", "wallet_buy")
            elif old_amount and amount < old_amount * 0.9:  # riduzione ≥10% = probabile vendita parziale/totale
                market = get_token_market_data(mint)
                symbol = market["symbol"] if market else mint[:6]
                if can_alert(state, f"{wallet}:{mint}", "wallet_sell"):
                    pct_sold = (1 - amount / old_amount) * 100
                    send_discord_alert(
                        webhook,
                        f"🐋 {label} ha venduto: {symbol}",
                        f"Ridotto di ~{pct_sold:.0f}% ({old_amount} → {amount})\nMint: {mint}",
                        color=0xFF4C4C,
                        fields=[{"name": "Wallet tracciato", "value": f"https://solscan.io/account/{wallet}", "inline": False}],
                    )
                    mark_alerted(state, f"{wallet}:{mint}", "wallet_sell")

        state["tracked_wallet_tokens"][wallet] = tokens


# ---------------------------------------------------------------------------
# Trading automatico (Jupiter swap) — ESEGUE TRANSAZIONI REALI
# ---------------------------------------------------------------------------
import base64

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
except ImportError:
    Keypair = None
    VersionedTransaction = None

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_URL = "https://price.jup.ag/v6/price"


def load_trading_keypair(config):
    """Carica la chiave privata SOLO da variabile d'ambiente, mai dal config.json."""
    trading = config.get("trading", {})
    if not trading.get("enabled"):
        return None
    if Keypair is None:
        print("[!] Libreria 'solders' non installata (pip install solders). Trading disattivato.")
        return None
    env_var = trading.get("private_key_env_var", "WALLET_PRIVATE_KEY")
    secret = os.environ.get(env_var)
    if not secret:
        print(f"[!] Trading abilitato ma variabile d'ambiente {env_var} non impostata. Trading disattivato.")
        return None
    try:
        return Keypair.from_base58_string(secret)
    except Exception as e:
        print(f"[!] Chiave privata non valida: {e}. Trading disattivato.")
        return None


def get_sol_price_usd():
    try:
        resp = requests.get(JUPITER_PRICE_URL, params={"ids": "SOL"}, timeout=10)
        return float(resp.json()["data"]["SOL"]["price"])
    except Exception:
        return None


def reset_daily_limit_if_needed(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_spent", {}).get("date") != today:
        state["daily_spent"] = {"date": today, "usd": 0.0}


def get_jupiter_quote(input_mint, output_mint, amount_raw, slippage_bps):
    params = {"inputMint": input_mint, "outputMint": output_mint,
              "amount": int(amount_raw), "slippageBps": slippage_bps}
    resp = requests.get(JUPITER_QUOTE_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_jupiter_swap_transaction(quote, user_pubkey, priority_fee_lamports):
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": priority_fee_lamports,
    }
    resp = requests.post(JUPITER_SWAP_URL, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["swapTransaction"]


def sign_and_send_swap(swap_tx_b64, keypair, rpc_url):
    raw_tx = base64.b64decode(swap_tx_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed = VersionedTransaction(tx.message, [keypair])
    serialized = base64.b64encode(bytes(signed)).decode()
    result = rpc_call(rpc_url, "sendTransaction",
                       [serialized, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    return result  # signature della transazione


def execute_buy(mint, config, state, keypair, market):
    """Compra 'max_usd_per_trade' dollari del token, rispettando il tetto giornaliero."""
    trading = config["trading"]
    webhook = config["discord_webhook_url"]
    reset_daily_limit_if_needed(state)
    spent_today = state["daily_spent"]["usd"]
    usd_amount = trading["max_usd_per_trade"]

    if spent_today + usd_amount > trading["max_usd_per_day"]:
        print(f"[i] Limite giornaliero raggiunto (${spent_today:.2f}), salto acquisto {market['symbol']}")
        return None

    sol_price = get_sol_price_usd()
    if not sol_price:
        print("[!] Impossibile ottenere prezzo SOL, acquisto annullato.")
        return None
    sol_amount = usd_amount / sol_price
    lamports = int(sol_amount * LAMPORTS_PER_SOL)

    try:
        quote = get_jupiter_quote(SOL_MINT, mint, lamports, trading["slippage_bps"])
        swap_tx = get_jupiter_swap_transaction(quote, str(keypair.pubkey()), trading.get("priority_fee_lamports", 0))
        sig = sign_and_send_swap(swap_tx, keypair, config["rpc_url"])
    except Exception as e:
        send_discord_alert(webhook, "❌ Acquisto FALLITO", f"Token: {market['symbol']}\nErrore: {e}", color=0xFF0000)
        return None

    state["daily_spent"]["usd"] = spent_today + usd_amount
    state.setdefault("trade_log", []).append(
        {"ts": time.time(), "type": "buy", "mint": mint, "usd": usd_amount, "sig": sig})

    send_discord_alert(
        webhook, f"✅ ACQUISTO ESEGUITO: {market['symbol']}",
        f"Speso: ${usd_amount:.2f} (~{sol_amount:.4f} SOL)\nPrezzo: ${market['price_usd']:.8f}\n"
        f"Tx: https://solscan.io/tx/{sig}",
        color=0x14F195,
    )
    return sig


def execute_sell(mint, token_amount, decimals, config, state, keypair, market):
    """Vende l'intera posizione (o la quantità indicata) del token."""
    trading = config["trading"]
    webhook = config["discord_webhook_url"]
    raw_amount = int(token_amount * (10 ** decimals))

    try:
        quote = get_jupiter_quote(mint, SOL_MINT, raw_amount, trading["slippage_bps"])
        swap_tx = get_jupiter_swap_transaction(quote, str(keypair.pubkey()), trading.get("priority_fee_lamports", 0))
        sig = sign_and_send_swap(swap_tx, keypair, config["rpc_url"])
    except Exception as e:
        send_discord_alert(webhook, "❌ Vendita FALLITA", f"Token: {market['symbol']}\nErrore: {e}", color=0xFF0000)
        return None

    out_sol = int(quote["outAmount"]) / LAMPORTS_PER_SOL
    state.setdefault("trade_log", []).append(
        {"ts": time.time(), "type": "sell", "mint": mint, "amount": token_amount, "sig": sig})

    send_discord_alert(
        webhook, f"✅ VENDITA ESEGUITA: {market['symbol']}",
        f"Venduto: {token_amount:g} token → ~{out_sol:.4f} SOL\nPrezzo: ${market['price_usd']:.8f}\n"
        f"Tx: https://solscan.io/tx/{sig}",
        color=0xFF9900,
    )
    return sig


# ---------------------------------------------------------------------------
# Segnali buy/sell
# ---------------------------------------------------------------------------

def check_sell_signals(config, state, owned_tokens, keypair=None):
    webhook = config["discord_webhook_url"]
    settings = config["signal_settings"]
    positions = config.get("positions", {})
    trading_enabled = config.get("trading", {}).get("enabled", False) and keypair is not None

    for mint, info in owned_tokens.items():
        amount = info["amount"]
        decimals = info["decimals"]
        market = get_token_market_data(mint)
        if not market or market["price_usd"] <= 0:
            continue

        update_price_history(state, mint, market["price_usd"], market["volume_h1"])
        history = state["price_history"][mint]

        entry_price = positions.get(mint, {}).get("entry_price_usd")
        if not entry_price:
            entry_price = state.setdefault("reference_price", {}).setdefault(mint, market["price_usd"])

        triggered, reasons = evaluate_sell_signal(market, history, entry_price, settings)
        if triggered and can_alert(state, mint, "sell"):
            if trading_enabled:
                execute_sell(mint, amount, decimals, config, state, keypair, market)
            else:
                send_discord_alert(
                    webhook,
                    f"🔻 Segnale VENDITA: {market['symbol']}",
                    "Motivi:\n- " + "\n- ".join(reasons) +
                    f"\n\nPrezzo attuale: ${market['price_usd']:.8f}\nQuantità posseduta: {amount}\n\n"
                    "⚠️ Segnale euristico, non è consiglio finanziario. Valuta tu.",
                    color=0xFF4C4C,
                    fields=[{"name": "Chart", "value": market["url"], "inline": False}],
                )
            mark_alerted(state, mint, "sell")


def check_buy_signals(config, state, owned_tokens, keypair=None):
    webhook = config["discord_webhook_url"]
    settings = config["signal_settings"]
    watchlist = config.get("watchlist", {})
    trading_enabled = config.get("trading", {}).get("enabled", False) and keypair is not None

    candidate_mints = set(m for m in watchlist if not m.startswith("_"))

    auto_discover = config.get("auto_discover", {})
    if auto_discover.get("enabled"):
        trending = discover_trending_tokens(auto_discover.get("scan_limit", 50))
        candidate_mints.update(trending)
        print(f"[i] Scan automatico: {len(trending)} token in tendenza aggiunti ai candidati.")

    for mint in candidate_mints:
        if mint in owned_tokens:
            continue  # salta token già posseduti

        market = get_token_market_data(mint)
        if not market or market["price_usd"] <= 0:
            continue

        update_price_history(state, mint, market["price_usd"], market["volume_h1"])
        history = state["price_history"][mint]

        triggered, reasons = evaluate_buy_signal(market, history, settings)
        if triggered and can_alert(state, mint, "buy"):
            is_discovered = mint not in watchlist
            if trading_enabled:
                execute_buy(mint, config, state, keypair, market)
            else:
                origin_note = "\n🔎 Trovato dallo scan automatico (non è nella tua watchlist)." if is_discovered else ""
                send_discord_alert(
                    webhook,
                    f"🚀 Segnale ACQUISTO: {market['symbol']}",
                    "Motivi:\n- " + "\n- ".join(reasons) +
                    f"\n\nPrezzo attuale: ${market['price_usd']:.8f}\nLiquidità: ${market['liquidity_usd']:,.0f}"
                    f"{origin_note}\n\n"
                    "⚠️ Segnale euristico, non è consiglio finanziario. Fai le tue verifiche prima di comprare — "
                    "specialmente se è un token trovato dallo scan automatico e non uno che conosci già.",
                    color=0x14F195,
                    fields=[{"name": "Chart", "value": market["url"], "inline": False}],
                )
            mark_alerted(state, mint, "buy")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle(config, state, keypair):
    """Un singolo ciclo di controllo: wallet, segnali, wallet tracciati."""
    owned_tokens, state = check_wallet(config, state)
    check_sell_signals(config, state, owned_tokens, keypair)
    check_buy_signals(config, state, owned_tokens, keypair)
    check_tracked_wallets(config, state)
    return state


def main():
    import sys
    run_once = "--once" in sys.argv  # modalità singola esecuzione, per GitHub Actions

    config = load_config()

    # Il webhook Discord può arrivare da variabile d'ambiente (consigliato se il
    # config.json finisce in una repository pubblica, es. GitHub Actions) e ha
    # sempre la precedenza sul valore scritto nel file.
    env_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_webhook:
        config["discord_webhook_url"] = env_webhook

    if "INSERISCI_QUI" in config["wallet_address"] or "INSERISCI_QUI" in config["discord_webhook_url"]:
        print("⚠️  Compila config.json (wallet, watchlist) e imposta DISCORD_WEBHOOK_URL prima di avviare.")
        raise SystemExit(1)

    state = load_state()
    keypair = load_trading_keypair(config)

    if keypair:
        trading = config["trading"]
        print(f"🔴 TRADING AUTOMATICO ATTIVO — max ${trading['max_usd_per_trade']}/trade, "
              f"max ${trading['max_usd_per_day']}/giorno. Esegue swap reali con fondi reali.")
    else:
        print("🟢 Modalità solo alert — nessuna transazione verrà eseguita automaticamente.")
    print("⚠️  Strumento euristico, non è consulenza finanziaria. Usalo come supporto, non come oracolo.")

    if run_once:
        print("Esecuzione singola (--once)...")
        try:
            state = run_cycle(config, state, keypair)
            save_state(state)
        except Exception as e:
            print(f"[!] Errore nel ciclo: {e}")
        print("Ciclo completato.")
        return

    print(f"Avvio bot su wallet {config['wallet_address']} — poll ogni {config['poll_interval_seconds']}s")
    while True:
        try:
            state = run_cycle(config, state, keypair)
            save_state(state)
        except Exception as e:
            print(f"[!] Errore nel ciclo principale: {e}")
        time.sleep(config["poll_interval_seconds"])


if __name__ == "__main__":
    if "--once" not in os.sys.argv:
        keep_alive()  # non serve per esecuzioni singole (GitHub Actions)
    main()

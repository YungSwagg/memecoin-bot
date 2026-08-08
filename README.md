# Memecoin Signal Bot (Solana / Phantom)

Bot di **sola lettura** — non tocca mai chiavi private o fondi. Fa due cose:

1. **Monitora il tuo wallet Phantom**: saldo SOL, token posseduti, transazioni.
2. **Genera segnali euristici buy/sell**:
   - 🔻 **VENDITA** per i token che già possiedi → take profit, stop loss, crollo improvviso, trend ribassista.
   - 🚀 **ACQUISTO** per i token in una tua watchlist (non ancora posseduti) → momentum, volume spike, trend rialzista.

Tutto arriva come alert su Discord.

## ⚠️ Prima di usarlo

Questo è uno strumento **euristico**, basato su regole tecniche configurabili (media mobile, variazione % di prezzo, volume). Non è consulenza finanziaria e non garantisce nulla: i memecoin sono estremamente volatili, spesso manipolati, e i pattern tecnici falliscono con frequenza molto più alta che su asset tradizionali. Trattalo come un assistente che ti fa notare cose, non come un sistema che decide al posto tuo.

## Setup

```bash
pip install requests --break-system-packages
python3 memecoin_bot.py   # prima esecuzione: crea config.json e si ferma
```

Apri `config.json` e compila:

- `wallet_address`: il tuo indirizzo Phantom
- `discord_webhook_url`: URL del webhook Discord (Impostazioni canale → Integrazioni → Webhook)
- `watchlist`: mint address dei memecoin che vuoi monitorare per segnali di acquisto (li trovi su DexScreener/Solscan/pump.fun)
- `positions` *(opzionale ma consigliato)*: prezzo medio a cui hai comprato ogni token che possiedi, per calcolare take profit/stop loss corretti. Se lo lasci vuoto, il bot usa come riferimento il primo prezzo che osserva (meno preciso).
- `signal_settings`: soglie che decidono quando scatta un segnale (vedi sotto)

Poi riavvia:
```bash
python3 memecoin_bot.py
```

## Come funzionano i segnali

**Acquisto (token in watchlist)** — serve che almeno 2 di questi 3 si verifichino insieme:
- prezzo salito ≥ `momentum_buy_pct_5m`% negli ultimi 5 minuti
- volume attuale ≥ `volume_spike_multiplier`x la media recente
- media mobile corta > media mobile lunga (trend up)

Token con liquidità sotto `min_liquidity_usd` vengono scartati a prescindere (troppo rischiosi/manipolabili).

**Vendita (token posseduti)** — basta **una** condizione per allertarti (approccio prudente):
- guadagno ≥ `take_profit_pct`% dal prezzo di riferimento
- perdita ≥ `stop_loss_pct`% dal prezzo di riferimento
- crollo ≥ `momentum_sell_drop_pct_5m`% in 5 minuti
- trend ribassista (media mobile corta < lunga)

Ogni segnale sullo stesso token ha un **cooldown di 30 minuti** per non spammarti.

## 🔴 Trading automatico (opzionale, disattivo di default)

Il bot può eseguire davvero gli swap (via [Jupiter](https://jup.ag), l'aggregatore DEX standard su Solana) invece di limitarsi a notificarti. **Va usato solo con un wallet dedicato, separato dal tuo wallet principale, con capitale che puoi permetterti di perdere interamente.**

### Setup

```bash
pip install solders base58 --break-system-packages
```

1. **Crea un wallet dedicato** (mai il tuo principale): in Phantom → menu account → "Add / Connect Wallet" → "Create New Wallet". Trasferisci lì solo il capitale destinato al bot (es. sotto i 100$ come hai indicato).

2. **Esporta la chiave privata** di quel wallet dedicato: Phantom → impostazioni account → "Show Private Key" (è la stringa base58, non la seed phrase da 12/24 parole).

3. **Impostala come variabile d'ambiente sul server**, MAI nel file config.json:
   ```bash
   export WALLET_PRIVATE_KEY="la_tua_chiave_privata_base58"
   ```
   Per farla persistere tra riavvii del server, aggiungila a `~/.bashrc` o, meglio, al file di servizio systemd (vedi sotto).

4. In `config.json`, sezione `trading`:
   ```json
   "trading": {
     "enabled": true,
     "private_key_env_var": "WALLET_PRIVATE_KEY",
     "max_usd_per_trade": 10,
     "max_usd_per_day": 30,
     "slippage_bps": 300,
     "priority_fee_lamports": 100000
   }
   ```
   - `max_usd_per_trade`: quanto spende per ogni singolo acquisto (le vendite chiudono l'intera posizione)
   - `max_usd_per_day`: tetto massimo di spesa giornaliera in acquisti — superato questo, il bot smette di comprare fino al giorno dopo, anche se arrivano altri segnali
   - `slippage_bps`: tolleranza di slippage (300 = 3%, ragionevole per memecoin volatili)

### Come funziona in pratica

- Con `trading.enabled=true`, i segnali 🚀 ACQUISTO e 🔻 VENDITA **non vengono più solo notificati**: vengono eseguiti come swap reali, e poi ricevi comunque una notifica Discord ✅ con l'esito e il link alla transazione su Solscan.
- Gli acquisti avvengono **solo** sui token della tua `watchlist` (mai su token "a caso" trovati altrove).
- Le vendite avvengono solo sui token che il bot vede effettivamente nel wallet dedicato.
- Se una transazione fallisce (slippage troppo alto, liquidità sparita, RPC lento), ricevi un alert ❌ con l'errore — il bot continua a girare, non si blocca.

### Kill switch

Per fermare istantaneamente ogni nuovo trade senza spegnere il monitoraggio: metti `"enabled": false` in `trading` dentro `config.json` e riavvia il bot (o, più veloce, killa il processo e riavvialo — non serve toccare altro).

### Cosa il bot NON fa

- Non gestisce prelievi o trasferimenti fuori dagli swap
- Non fa sizing dinamico basato sul rischio percepito — spende sempre `max_usd_per_trade` fisso
- Non ha protezione anti-rug-pull nativa: se un token diventa illiquido tra il segnale e l'esecuzione, lo swap fallisce (e te lo notifica) ma non c'è garanzia di uscita in tempo da un rug in corso

## ☁️ Hosting su GitHub Actions (gratis, no carta di credito)

Il bot gira sui server di GitHub a intervalli programmati (ogni 15 minuti di default), senza bisogno di PC, telefono o VPS sempre accesi.

### Come funziona

A differenza della modalità "loop continuo" (PC/Termux), qui lo script viene lanciato con il flag `--once`: fa un singolo giro di controlli e si ferma. GitHub Actions lo rilancia da zero ogni 15 minuti secondo lo schedule nel file `.github/workflows/monitor.yml`. Lo stato tra un'esecuzione e l'altra (prezzi storici, cooldown alert, ecc.) è salvato in `state.json`, che il workflow committa automaticamente nella repository ad ogni esecuzione.

### Setup

1. **Crea una repository GitHub** (github.com → New repository) — deve essere **pubblica** per avere minuti di esecuzione illimitati gratis (con una repo privata il piano free dà solo 2.000 minuti/mese, che con un giro ogni 15 min si esauriscono presto).

   ⚠️ **Pubblica** significa che chiunque può vedere il codice e `config.json` — per questo il webhook Discord NON va messo nel config, ma in un "secret" nascosto (punto 3).

2. **Carica tutti i file** in questa repository, mantenendo la struttura:
   ```
   memecoin_bot.py
   config.json
   requirements.txt
   .github/workflows/monitor.yml
   ```
   Puoi farlo dal browser: nella repo, "Add file" → "Upload files" → trascina tutti i file (per `.github/workflows/monitor.yml` dovrai prima creare le cartelle usando "Create new file" e scrivendo il percorso completo nel nome).

3. **Aggiungi il webhook Discord come secret**: nella repo → Settings → Secrets and variables → Actions → **New repository secret**
   - Nome: `DISCORD_WEBHOOK_URL`
   - Valore: il tuo URL webhook Discord

4. **Verifica `config.json`**: lascia `discord_webhook_url` con il placeholder (`INSERISCI_QUI...`) — verrà sovrascritto automaticamente dal secret. Wallet address, watchlist e tracked_wallets restano nel file normalmente (sono dati pubblici on-chain, nessun problema a mostrarli).

5. **Abilita le Actions**: tab "Actions" della repo → se richiesto, clicca "I understand my workflows, go ahead and enable them"

6. **Test manuale**: tab "Actions" → "Memecoin Bot Monitor" → "Run workflow" → Run workflow. Dopo ~1 minuto controlla i log (clicca sull'esecuzione) e il tuo Discord per il primo alert di verifica.

Da qui in poi gira da solo ogni 15 minuti, 24/7, senza che tu debba tenere acceso nulla.

### Limiti da conoscere

- **Trading automatico non è compatibile con questa modalità** — richiede stato continuo (limite giornaliero di spesa, cooldown) che con esecuzioni intermittenti ogni 15 minuti diventa meno affidabile da gestire in sicurezza. Se vuoi il trading automatico, serve un hosting con processo continuo (VPS, Termux).
- Gli orari del cron di GitHub non sono precisissimi nei momenti di traffico alto sulla piattaforma — a volte un ciclo può slittare di qualche minuto.
- Il file `state.json` cresce nel tempo col commit automatico; se dopo mesi noti la repo appesantita, è normale, puoi ripulire la history se ti dà fastidio.

## 🔎 Scan automatico dei token in tendenza (opzionale)

Oltre alla watchlist statica, il bot può scaricare ogni ciclo i ~50 token più attivi su **pump.fun** (dove nascono la maggior parte dei memecoin Solana) e applicare gli stessi filtri di acquisto anche a loro.

⚠️ **Rischio più alto**: a differenza della watchlist, questi sono token che non conosci e non hai scelto tu — possono essere freschissimi, senza storia, con probabilità di rug pull molto più alta. I filtri (`min_liquidity_usd`, momentum, volume spike, trend) scartano i casi più ovvi ma non garantiscono nulla.

Attiva/disattiva in `config.json`:
```json
"auto_discover": {
  "enabled": true,
  "scan_limit": 50
}
```

Gli alert generati da questa modalità hanno una nota extra: "🔎 Trovato dallo scan automatico (non è nella tua watchlist)" — così sai sempre distinguere un segnale su un token che conosci da uno completamente nuovo.

Nota tecnica: usa un endpoint non ufficiale di pump.fun che potrebbe cambiare o smettere di funzionare senza preavviso — se succede, il bot logga l'errore e continua a funzionare normalmente solo con la watchlist statica.

## 🔀 Canali Discord separati (opzionale)

Puoi ricevere gli alert su 3 canali Discord diversi invece che tutti nello stesso:
- **wallet** → alert sul tuo wallet Phantom (saldo, token, transazioni)
- **signals** → segnali buy/sell (watchlist + scan automatico + notifiche di trade eseguiti)
- **tracked_wallets** → alert sui wallet di terzi che tracci

**In locale (PC/Termux)**: compila `discord_webhooks` in `config.json` con i 3 URL webhook (uno per ogni canale Discord che crei). Se lasci una categoria vuota/placeholder, quella categoria usa `discord_webhook_url` come fallback.

**Su GitHub Actions**: più pulito usare secret dedicati invece di scrivere gli URL nel config (anche se `discord_webhooks` di per sé può stare nel config, dato che il repo è pubblico è comunque meglio tenerli come secret):
- `DISCORD_WEBHOOK_WALLET`
- `DISCORD_WEBHOOK_SIGNALS`
- `DISCORD_WEBHOOK_TRACKED`
- `DISCORD_WEBHOOK_URL` (fallback generico, usato per categorie senza secret dedicato)

Questi secret hanno sempre precedenza sui valori scritti in `config.json`. Aggiungili nella stessa pagina dove hai già messo `DISCORD_WEBHOOK_URL` (Settings → Secrets and variables → Actions → New repository secret).

## 📢 Ping @everyone sulle vendite

Le notifiche legate alla vendita (segnale di vendita, vendita eseguita, vendita fallita, token sparito dal wallet) includono automaticamente un ping `@everyone` per farle risaltare.

⚠️ Perché funzioni, il webhook deve avere il permesso di menzionare `@everyone` nel canale Discord: di solito è già così di default, ma se non vedi il ping arrivare, controlla in Discord → Impostazioni canale → Permessi, che il ruolo del webhook (o `@everyone` stesso) abbia il permesso "Menziona @everyone, @here e Tutti i ruoli" abilitato.

## Note tecniche

- Prezzi/volumi da **DexScreener** (pubblica, no API key, ma non garantita al 100% per token appena lanciati).
- RPC Solana pubblico di default: per uso continuo conviene un endpoint gratuito [Helius](https://www.helius.dev/) (sostituisci `rpc_url`).
- Stato salvato in `state.json`: storico prezzi, ultimo saldo, cooldown alert. Cancellalo se vuoi ripartire da zero.

## Eseguirlo in background

```bash
nohup python3 memecoin_bot.py > bot.log 2>&1 &
```

Per farlo girare 24/7 in modo affidabile: un piccolo VPS (Hetzner, Oracle free tier) o Railway/Render, non il tuo PC.

# Trade Execution on Pump Signals – PRD

Extension of the existing Pump Detector Telegram bot. When a pump alert fires, the bot
automatically opens a **short** position on Bybit USDT perpetual futures, with take-profit
and stop-loss attached. The detection side (collector, thresholds, windows) already exists
and is reused as-is.

## 1. Trigger

- A trade is triggered by the **admin's pump signal** — the same event that produces the
  admin's pump alert (admin's pump threshold + pump time window).
- Dump signals do **not** trigger trades.
- admin's chat ID is in the ENV file.
- Trading is active only when **both** are true: trading is enabled (`/start_trading`
  completed, not paused via `/stop_trading`) **and** the admin's alerts are not paused
  (`/pause` pauses trading as well; `/resume` resumes it).

## 2. Entry order

On a qualifying pump signal, place a **limit sell (short)** order:

- **Limit price** = trigger price × (1 − OFFSET), where the trigger price is the current
  mark price at the tick that triggered the alert.
- The order is expected to **fill immediately at market**; OFFSET is the maximum
  acceptable slippage, not a discount to wait for.
- **Size**: SHORT_SIZE notional in USDT → quantity = SHORT_SIZE / limit price, rounded
  per symbol rules (see §6).
- **TP and SL are attached to the entry order itself** (Bybit `takeProfit` / `stopLoss`
  parameters), not placed as separate reduce-only orders. This is atomic — no window
  where a position exists without TP/SL — and auto-handles partial fills.
  - For an entry limit price P: **TP price = P × (1 − TP_SIZE)**, **SL price = P × (1 + SL_SIZE)**.
- **Leverage 10x, cross margin**, one-way position mode.
- **ORDER_TTL**: if the order is not filled within ORDER_TTL minutes, the bot cancels it.

## 3. Position rules

- **One position per symbol.** Skip the signal if a position or a pending entry order
  already exists for that symbol.
- **MAX_OPEN_POSITIONS** counts **all** open positions in the linear perps account —
  both bot-opened and positions the admin opened manually via the Bybit app. If the cap
  is reached, skip the signal.
- The bot distinguishes its own positions from manual ones (via `orderLinkId` and the
  trades table). It counts all positions toward the cap, but **only ever manages or
  closes its own**.
- **Skip behavior**: whenever a signal is skipped (cap reached, position already open in
  the symbol, symbol constraints unmet) — notify the admin with the reason.

## 4. Parameters

Set via the `/start_trading` six-step inline-keyboard flow (consistent with the rest of
the bot; preset choices only, no free-text input):

| # | Parameter | Meaning | Choices |
|---|-----------|---------|---------|
| 1 | OFFSET | Max slippage below trigger price, % | 0, 1, 2, 3 |
| 2 | SHORT_SIZE | Notional size per trade, USDT | 100, 200, 300, 400, 500 |
| 3 | TP_SIZE | Take-profit distance below entry, % | 3, 5, 7, 10, 15 |
| 4 | ORDER_TTL | Cancel unfilled entry after, minutes | 10, 15, 30, 60, 180 |
| 5 | SL_SIZE | Stop-loss distance above entry, % | 5, 7, 10, 15 |
| 6 | MAX_OPEN_POSITIONS | Cap on total open positions (bot + manual) | 5, 10, 20 |

## 5. Commands

| Command | Behavior |
|---------|----------|
| `/start_trading` | Admin only. Runs the 6-step parameter flow, saves the parameters, and enables trading. |
| `/stop_trading` | Pauses trading: alerts continue, no new orders are placed. Pending entry orders and open positions **stay active** and continue to be managed (TTL cancellation, TP/SL monitoring, notifications keep running). Note: a pending entry may therefore still fill after pausing. |
| `/trading_status` | Shows: current parameters, trading on/off state, open bot positions with unrealized PnL, realized PnL for the current UTC day. |

To re-enable trading after `/stop_trading`, the admin runs `/start_trading` again and
re-answers all six keyboards.

## 6. Execution details

- **Symbol constraints**: round quantity to the symbol's qty step and prices to its tick
  size (from Bybit instruments-info). If SHORT_SIZE cannot satisfy the symbol's min
  qty / min notional, skip the trade and notify.
- **Idempotency**: every signal generates a deterministic `orderLinkId` (e.g. derived
  from symbol + signal timestamp), so API retries or bot restarts can never open a
  duplicate order for the same signal.
- **Fill/TTL tracking**: Bybit has no native "cancel after N minutes". The bot polls
  open orders and positions on each collector tick to detect entry fills, TP/SL hits,
  and to enforce ORDER_TTL. Polling per tick is sufficient for v1 (no private
  websocket).
- TTL enforcement and position monitoring keep running while trading is paused; only
  the placement of **new** orders stops.

## 7. Persistence & restart reconciliation

- New **`trades` table** in the existing SQLite DB: signal data (symbol, trigger price,
  timestamp), `orderLinkId`, exchange order ID, entry/TP/SL prices, quantity, status
  (pending / open / tp_hit / sl_hit / cancelled / error), realized PnL.
- Reconciliation, notifications and `/trading_status` all read from this table.
- **On startup**, query Bybit for open orders and positions and reconcile them with the
  trades table (the bot will restart with positions open; orders that were filled,
  cancelled or closed while the bot was down must be resolved and notified).

## 8. Access control & secrets

- Trading is available **only to the bot admin**. The admin's Telegram chat ID is
  hardcoded in config; only that ID can use `/start_trading`, `/stop_trading`,
  `/trading_status`.
- Bybit API key/secret live in `.env` next to the bot token.
- Deployment requirement: the API key must have **trade permission only** (no
  withdrawals) and be IP-whitelisted to the server.

## 9. Notifications

The admin receives a Telegram message on:

- entry order filled,
- TP hit (with realized PnL),
- SL hit (with realized PnL),
- order expired (TTL) or cancelled,
- signal skipped (with reason),
- any error (order rejected, API failure, reconciliation mismatch, …).

## 10. Example

The admin runs `/start_trading` and selects:

```
OFFSET             = 1%
SHORT_SIZE         = 300 USDT
TP_SIZE            = 7%
ORDER_TTL          = 10 min
SL_SIZE            = 7%
MAX_OPEN_POSITIONS = 10
```

When a pump signal fires with trigger price P:

- A limit sell order is placed at P × 0.99 (1% below the trigger price), expected to
  fill immediately; 1% is the worst acceptable fill.
- Order size: 300 USDT notional.
- Take profit attached at entry × 0.93 (−7%).
- Stop loss attached at entry × 1.07 (+7%).
- If unfilled after 10 minutes, the order is cancelled.
- No new trade is opened if 10 positions are already open on the account, or if a
  position already exists in that symbol.

## 11. Out of scope for v1 (deliberate decisions)

- **No dry-run / testnet mode** — trading goes straight to mainnet.
- **No kill switch** (max-daily-loss auto-pause) and no `/close_all` command; SL_SIZE is
  the only automated loss control. Note: with cross margin, a failure of the SL
  mechanism exposes the whole account balance.
- **Isolated margin rejected** in favor of cross margin, 10x, fixed (not configurable).

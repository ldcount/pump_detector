

I am writing a python bot for telegram that that I would call “pump detector”. It monitors Bybit USDT-margined perpetual futures symbols. It is supposed to update me on the extreme pumping events on Bybit with futures / perps. Extreme pumping = when a particular coin pump big over a pre-defined period of time, modifiable by user. The threshold of what is a big pump is also modifiable by user through telegram. When an extreme pumping happens, the bot sends an alert to telegram in the following format: 

🟢Pump: <COIN_NAME>: <PUMP_PERCENTAGE>

<COIN_NAME> must be a clickable link to the symbol’s Bybit perp trading page using an explicit URL template.

Here is the functionality:

1)	Bot scans every 30 seconds (frequency parameter) the prices for every perp available on bybit. Frequency of the scan shall be modifiable by user. 
2)	Logs in the price in local database. Thus, this bot will work as a price logger across all coins it scans. 
3)	Bot has 3 different parameters for each user, i.e. each bot’s user can modify these parameters at his/her convenience:
a.	frequency of the scan (as in point 1), scan frequency can not be below 30 seconds 
b.	pumping threshold, i.e. 10%, 15%, 100%, etc – the pumping threshold upon which an alert is sent to the telegram. Pumping thresholds can be chosen by user on a pre-defined basis: 10%, 20%, 30%, 50%, 80%.
c.	time threshold in minutes – the threshold against which the pump event shall be registered, modifiable by each user. Time thresholds can be chosen by user on a pre-defined basis: 5, 10, 15, 20, 30, 40, 60 min. 
4)	UX details: start ask 3 questions in sequence: frequency, pumping threshold, time threshold

Additional guidelines:
1)	When coding, use mark price (preferred) as the basis.  
2)	Alert condition per user: if pump_pct >= pump_threshold_pct
3)	Pump in percentages formula: pump_pct = (current_price / price_X_minutes_ago - 1) * 100
4)	You can use SQLite as DB. The DB will store the price logs per symbol key. DB will store the settings per user. Again: price history stored globally.
5)	I want a single market data collector (global), computation of pump events per symbol/window, then fan-out alerts to users whose settings match. 
6)	There shall be a /help command describing how the bot works
7)	There shall be a /status command describing the current parameters that are set
8)	There shall be a /pause command pausing the bot running for the user
9)	There shall be a /resume command to restart the bot when it was paused
10)	The logging shall be done only for errors, start, pausing and restarting, info on startup 
11)	The bot will be run in venv environment in Python (no Docker)
12)	The bot will be run later on via systemd
13)	Secrets, if needed, will be stored in .env 
14)	Storage retention: keep logs of prices for 72 hours, then purge to keep the DB small

Tech stack:
1)	Use python-telegram-bot lib for telegram functionality
2)	Use pybit lib for Bybit access 
3)	Important architectural note:
a.	Collect prices once globally.
b.	Compute pump metrics globally per symbol.
c.	Send alerts per user based on their thresholds.
4)	The bot api key to put in .env is: XXX



Example: let’s say that I set the following parameters: 
Frequency: 30
Pumping threshold: 30%
Time threshold: 15 

If a particular coin, say PIPPIN, pumps 36,32% in 10 minutes, I’ll be alerted in my telegram with the following message:
🟢Pump: PIPPIN: 36.32%

If three or more coins pumped over 30%, I’ll receive 3 separate messages.

The three parameters shall be set upon bot start, the user shall be prompted to set up them. 

The modification of the parameters shall be possible via /param command. 






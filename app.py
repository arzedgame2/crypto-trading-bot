# ---------- IMPORTS ----------
import os, time, json, io, requests, aiohttp
import pandas as pd, numpy as np, matplotlib.pyplot as plt
from datetime import datetime
import ta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ---------- JSON HELPERS ----------
def load_json(path):
    if not os.path.exists(path): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {}

def save_json(path, obj):
    try:
        with open(path, 'w', encoding='utf-8') as f: json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"ERR save_json {path}: {e}")

# ---------- GLOBAL STATE ----------
user_prefs = load_json("user_prefs.json")
performance_log = load_json("performance_log.json")
user_exchanges = load_json("user_exchanges.json")

# ---------- CACHE ----------
_cache = {}
def cached(ttl=90):
    def decorator(func):
        def wrapper(*args, **kwargs):
            key = (func.__name__, args, tuple(sorted(kwargs.items())))
            now = time.time()
            entry = _cache.get(key)
            if entry and now - entry['t'] < ttl: return entry['v']
            v = func(*args, **kwargs)
            _cache[key] = {'v': v, 't': now}
            return v
        return wrapper
    return decorator

# ---------- PREFS & PERFORMANCE ----------
def get_user_prefs(chat_id):
    prefs = user_prefs.get(str(chat_id))
    if not prefs:
        prefs = {"balance": 1000.0, "risk": 0.02, "language": "fa", "notifications": True}
        user_prefs[str(chat_id)] = prefs
        save_json("user_prefs.json", user_prefs)
    return prefs

def set_user_prefs(chat_id, balance=None, risk=None, language=None, notifications=None):
    prefs = get_user_prefs(chat_id)
    if balance is not None: prefs["balance"] = max(10.0, float(balance))
    if risk is not None: prefs["risk"] = float(risk)
    if language is not None: prefs["language"] = language
    if notifications is not None: prefs["notifications"] = notifications
    user_prefs[str(chat_id)] = prefs
    save_json("user_prefs.json", user_prefs)
    return prefs

def log_trade(chat_id, record):
    arr = performance_log.get(str(chat_id), [])
    arr.append(record)
    performance_log[str(chat_id)] = arr
    save_json("performance_log.json", performance_log)

def summarize_performance(chat_id):
    arr = performance_log.get(str(chat_id), [])
    if not arr: return "🔎 هنوز هیچ رکورد عملکردی ثبت نشده."
    pnl_sum = sum(x.get("pnl", 0) for x in arr)
    wins = sum(1 for x in arr if x.get("pnl", 0) > 0)
    losses = sum(1 for x in arr if x.get("pnl", 0) < 0)
    count = len(arr)
    avg_pnl = pnl_sum / count if count else 0
    win_rate = (wins / count * 100) if count else 0
    return f"""📈 خلاصه عملکرد شما:

💰 سود/ضرر کل: {pnl_sum:+.2f} USD
📊 تعداد معاملات: {count}
✅ معاملات سودده: {wins}
❌ معاملات ضررده: {losses}
🎯 نرخ برد: {win_rate:.1f}%
📦 میانگین سود/معامله: {avg_pnl:+.2f} USD"""

# ---------- ALERTS ----------
class AlertManager:
    def __init__(self):
        self.price_alerts = load_json("price_alerts.json")          # {chat_symbol_key: [ {level,type,created,triggered} ]}
        self.indicator_alerts = load_json("indicator_alerts.json")  # {chat_symbol_key: [ {type,created,triggered} ]}
        self.tracking_positions = load_json("tracking_positions.json")  # {chat_symbol_key: {entry,size,direction,tp_hit,trailing_stop}}

    def add_price_alert(self, chat_id, symbol, level, alert_type="price_above"):
        key = f"{chat_id}_{symbol.upper()}"
        self.price_alerts.setdefault(key, [])
        self.price_alerts[key].append({
            "level": float(level),
            "type": alert_type,
            "created": datetime.now().isoformat(),
            "triggered": False
        })
        save_json("price_alerts.json", self.price_alerts)
        return True

    def add_indicator_alert(self, chat_id, symbol, indicator_type):
        key = f"{chat_id}_{symbol.upper()}"
        self.indicator_alerts.setdefault(key, [])
        self.indicator_alerts[key].append({
            "type": indicator_type,
            "created": datetime.now().isoformat(),
            "triggered": False
        })
        save_json("indicator_alerts.json", self.indicator_alerts)
        return True

    def start_tracking(self, chat_id, symbol, entry, size, direction, trailing_stop=None):
        key = f"{chat_id}_{symbol.upper()}"
        self.tracking_positions[key] = {
            "entry": float(entry), "size": float(size), "direction": direction.upper(),
            "tp_hit": False, "trailing_stop": trailing_stop
        }
        save_json("tracking_positions.json", self.tracking_positions)

    def stop_tracking(self, chat_id, symbol):
        key = f"{chat_id}_{symbol.upper()}"
        if key in self.tracking_positions:
            del self.tracking_positions[key]
            save_json("tracking_positions.json", self.tracking_positions)

    def _check_indicator_alert(self, alert_type, indicators):
        if not indicators: return False
        # indicators: dict like {"rsi": float, "macd": float, "macd_signal": float}
        if alert_type == "MACD_CROSS_UP":
            return indicators.get("macd") is not None and indicators.get("macd_signal") is not None and indicators["macd"] > indicators["macd_signal"]
        if alert_type == "MACD_CROSS_DOWN":
            return indicators.get("macd") is not None and indicators.get("macd_signal") is not None and indicators["macd"] < indicators["macd_signal"]
        if alert_type == "RSI_OVERBOUGHT":
            return indicators.get("rsi") is not None and indicators["rsi"] > 70
        if alert_type == "RSI_OVERSOLD":
            return indicators.get("rsi") is not None and indicators["rsi"] < 30
        return False

    def check_alerts(self, chat_id, symbol, current_price, indicators=None, df=None, send_fn=None):
        key = f"{chat_id}_{symbol.upper()}"
        triggered_msgs = []

        # Price alerts
        for alert in self.price_alerts.get(key, []):
            if alert["triggered"]: continue
            if (alert["type"] == "price_above" and current_price >= alert["level"]) or \
               (alert["type"] == "price_below" and current_price <= alert["level"]):
                alert["triggered"] = True
                msg = f"🔔 هشدار قیمت {symbol}: {'عبور به بالا' if alert['type']=='price_above' else 'عبور به پایین'} از ${alert['level']:,.2f} (قیمت فعلی: ${current_price:,.2f})"
                triggered_msgs.append(msg)
                if send_fn: send_fn(chat_id, msg)

        # Indicator alerts
        for alert in self.indicator_alerts.get(key, []):
            if alert["triggered"]: continue
            if self._check_indicator_alert(alert["type"], indicators):
                alert["triggered"] = True
                msg = f"🔔 هشدار اندیکاتور {symbol}: {alert['type']}"
                triggered_msgs.append(msg)
                if send_fn: send_fn(chat_id, msg)

        # Trailing TP tracking
        pos = self.tracking_positions.get(key)
        if pos:
            entry = pos["entry"]; size = pos["size"]; direction = pos["direction"]; trailing = pos["trailing_stop"]
            if direction == "LONG":
                tp1 = entry * 1.05
                if current_price >= tp1 and not pos["tp_hit"]:
                    pos["tp_hit"] = True
                    new_ts = None
                    if df is not None and not df.empty:
                        last = df.iloc[-1]
                        ema20 = last.get('sma_20', None)
                        atr = last.get('atr', None)
                        if ema20: new_ts = float(ema20)
                        elif atr: new_ts = round(entry + float(atr), 4)
                    pos["trailing_stop"] = new_ts or round(entry * 1.01, 4)
                    msg = f"✅ {symbol}: TP1 رسید. 50% خروج. حدضرر دنبال‌کننده: ${pos['trailing_stop']}"
                    triggered_msgs.append(msg); 
                    if send_fn: send_fn(chat_id, msg)
                    pnl = (tp1 - entry) * (size * 0.5)
                    log_trade(chat_id, {"symbol": symbol, "entry": entry, "exit": tp1, "pnl": pnl, "time": datetime.now().isoformat(), "note": "TP1 50%"})
                if pos["tp_hit"] and trailing and current_price <= trailing:
                    exit_price = current_price
                    pnl = (exit_price - entry) * (size * 0.5)
                    log_trade(chat_id, {"symbol": symbol, "entry": entry, "exit": exit_price, "pnl": pnl, "time": datetime.now().isoformat(), "note": "Trailing exit"})
                    msg = f"⏹️ {symbol}: خروج کامل با تریلینگ در ${exit_price:,.2f}"
                    triggered_msgs.append(msg); 
                    if send_fn: send_fn(chat_id, msg)
                    self.stop_tracking(chat_id, symbol)
            elif direction == "SHORT":
                tp1 = entry * 0.95
                if current_price <= tp1 and not pos["tp_hit"]:
                    pos["tp_hit"] = True
                    new_ts = None
                    if df is not None and not df.empty:
                        last = df.iloc[-1]
                        ema20 = last.get('sma_20', None)
                        atr = last.get('atr', None)
                        if ema20: new_ts = float(ema20)
                        elif atr: new_ts = round(entry - float(atr), 4)
                    pos["trailing_stop"] = new_ts or round(entry * 0.99, 4)
                    msg = f"✅ {symbol}: TP1 رسید (SHORT). 50% خروج. حدضرر دنبال‌کننده: ${pos['trailing_stop']}"
                    triggered_msgs.append(msg); 
                    if send_fn: send_fn(chat_id, msg)
                    pnl = (entry - tp1) * (size * 0.5)
                    log_trade(chat_id, {"symbol": symbol, "entry": entry, "exit": tp1, "pnl": pnl, "time": datetime.now().isoformat(), "note": "TP1 50% (SHORT)"})
                if pos["tp_hit"] and trailing and current_price >= trailing:
                    exit_price = current_price
                    pnl = (entry - exit_price) * (size * 0.5)
                    log_trade(chat_id, {"symbol": symbol, "entry": entry, "exit": exit_price, "pnl": pnl, "time": datetime.now().isoformat(), "note": "Trailing exit (SHORT)"})
                    msg = f"⏹️ {symbol}: خروج کامل با تریلینگ در ${exit_price:,.2f}"
                    triggered_msgs.append(msg); 
                    if send_fn: send_fn(chat_id, msg)
                    self.stop_tracking(chat_id, symbol)

        # Save changes
        if triggered_msgs:
            save_json("price_alerts.json", self.price_alerts)
            save_json("indicator_alerts.json", self.indicator_alerts)
            save_json("tracking_positions.json", self.tracking_positions)
        return triggered_msgs

    def get_user_alerts(self, chat_id):
        user_alerts = []
        chat_id_str = str(chat_id)
        for key, alerts in self.price_alerts.items():
            if key.startswith(chat_id_str + "_"):
                user_alerts.extend([{"type": "price", **alert} for alert in alerts if not alert["triggered"]])
        for key, alerts in self.indicator_alerts.items():
            if key.startswith(chat_id_str + "_"):
                user_alerts.extend([{"type": "indicator", **alert} for alert in alerts if not alert["triggered"]])
        return user_alerts

alerts = AlertManager()

# ---------- ANALYZER ----------
class AdvancedCryptoAnalyzer:
    def __init__(self):
        self.base_url = "https://api.coingecko.com/api/v3"
        self.symbol_map = {
            'BTC': 'bitcoin', 'ETH': 'ethereum', 'BNB': 'binancecoin', 'XRP': 'ripple',
            'ADA': 'cardano', 'SOL': 'solana', 'DOGE': 'dogecoin', 'TRX': 'tron',
            'TON': 'toncoin', 'LINK': 'chainlink', 'AVAX': 'avalanche-2', 'DOT': 'polkadot',
            'MATIC': 'matic-network', 'NEAR': 'near', 'BCH': 'bitcoin-cash', 'ICP': 'internet-computer',
            'LTC': 'litecoin', 'UNI': 'uniswap', 'XLM': 'stellar', 'ATOM': 'cosmos'
        }

    def symbol_to_id(self, symbol):
        return self.symbol_map.get(symbol.upper())

    @cached(ttl=120)
    def get_ohlc_data(self, symbol, days=90):
        try:
            coin_id = self.symbol_to_id(symbol)
            if not coin_id: return None
            url = f"{self.base_url}/coins/{coin_id}/ohlc"
            params = {'vs_currency': 'usd', 'days': days}
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df.set_index('timestamp')
        except Exception as e:
            print(f"خطا در دریافت داده‌های {symbol}: {e}")
            return None

    def calculate_indicators(self, df):
        try:
            df['rsi_14'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            df['rsi_21'] = ta.momentum.RSIIndicator(df['close'], window=21).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_signal'] = macd.macd_signal()
            df['macd_histogram'] = macd.macd_diff()
            df['sma_20'] = ta.trend.SMAIndicator(df['close'], window=20).sma_indicator()
            df['sma_50'] = ta.trend.SMAIndicator(df['close'], window=50).sma_indicator()
            df['sma_100'] = ta.trend.SMAIndicator(df['close'], window=100).sma_indicator()
            bollinger = ta.volatility.BollingerBands(df['close'])
            df['bb_upper'] = bollinger.bollinger_hband()
            df['bb_lower'] = bollinger.bollinger_lband()
            df['bb_middle'] = bollinger.bollinger_mavg()
            stoch = ta.momentum.StochasticOscillator(df['high'], df['low'], df['close'])
            df['stoch_k'] = stoch.stoch()
            df['stoch_d'] = stoch.stoch_signal()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
            df.dropna(inplace=True)
            return df
        except Exception as e:
            print(f"خطا در محاسبه اندیکاتورها: {e}")
            return df

    def generate_signals(self, df):
        if df is None or df.empty:
            return {"signals": [], "overall_signal": "⚪ بازار خنثی", "action": "منتظر بمانید", "confidence": 0, "current_price": 0.0}
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        signals = []
        confidence = 0
        if latest['rsi_14'] < 30 and latest['rsi_21'] < 35:
            signals.append("🟢 RSI در ناحیه اشباع فروش"); confidence += 2
        elif latest['rsi_14'] > 70 and latest['rsi_21'] > 65:
            signals.append("🔴 RSI در ناحیه اشباع خرید"); confidence -= 2
        if latest['macd'] > latest['macd_signal'] and prev['macd'] <= prev['macd_signal']:
            signals.append("🟢 MACD کراس صعودی"); confidence += 1
        elif latest['macd'] < latest['macd_signal'] and prev['macd'] >= prev['macd_signal']:
            signals.append("🔴 MACD کراس نزولی"); confidence -= 1
        if latest['sma_20'] > latest['sma_50'] > latest['sma_100']:
            signals.append("🟢 روند صعودی قوی"); confidence += 2
        elif latest['sma_20'] < latest['sma_50'] < latest['sma_100']:
            signals.append("🔴 روند نزولی قوی"); confidence -= 2
        if latest['close'] < latest['bb_lower']:
            signals.append("🟢 قیمت در کف باند بولینگر"); confidence += 1
        elif latest['close'] > latest['bb_upper']:
            signals.append("🔴 قیمت در سقف باند بولینگر"); confidence -= 1
        if latest['stoch_k'] < 20 and latest['stoch_d'] < 20:
            signals.append("🟢 استوکاستیک در ناحیه فروش"); confidence += 1
        elif latest['stoch_k'] > 80 and latest['stoch_d'] > 80:
            signals.append("🔴 استوکاستیک در ناحیه خرید"); confidence -= 1

        if confidence >= 3:
            overall_signal = "🟢 سیگنال خرید قوی"; action = "خرید"
        elif confidence >= 1:
            overall_signal = "🟡 سیگنال خرید ضعیف"; action = "صبر کنید"
        elif confidence <= -3:
            overall_signal = "🔴 سیگنال فروش قوی"; action = "فروش"
        elif confidence <= -1:
            overall_signal = "🟠 سیگنال فروش ضعیف"; action = "احتیاط"
        else:
            overall_signal = "⚪ بازار خنثی"; action = "منتظر بمانید"

        return {"signals": signals, "overall_signal": overall_signal, "action": action,
                "confidence": confidence, "current_price": float(latest['close'])}

    def calculate_support_resistance(self, df):
        try:
            latest = df.iloc[-1]
            pivot = (latest['high'] + latest['low'] + latest['close']) / 3
            r1 = 2 * pivot - latest['low']
            s1 = 2 * pivot - latest['high']
            supports = [latest['bb_lower'], latest['sma_50'], s1]
            resistances = [latest['bb_upper'], latest['sma_20'], r1]
            supports = [round(float(x), 2) for x in supports if not np.isnan(x)]
            resistances = [round(float(x), 2) for x in resistances if not np.isnan(x)]
            return {"support": sorted(supports)[:3], "resistance": sorted(resistances)[:3]}
        except Exception as e:
            print(f"خطا در محاسبه سطوح: {e}")
            return {"support": [], "resistance": []}

    def analyze_crypto(self, symbol):
        df = self.get_ohlc_data(symbol)
        if df is None or df.empty: return None
        df = self.calculate_indicators(df)
        signal_data = self.generate_signals(df)
        levels = self.calculate_support_resistance(df)
        price = signal_data['current_price']
        atr = df['atr'].iloc[-1] if 'atr' in df.columns and not df['atr'].isna().iloc[-1] else None
        if signal_data['confidence'] > 0:
            targets = [round(price * 1.05, 2), round(price * 1.10, 2), round(price * 1.15, 2)]
            stop_loss = round(price * 0.95, 2) if atr is None else round(price - 1.5 * atr, 2)
        else:
            targets = []
            stop_loss = round(price * 1.05, 2) if atr is None else round(price + 1.5 * atr, 2)
        return {"symbol": symbol.upper(), "current_price": price, "signal_data": signal_data, "levels": levels,
                "targets": targets, "stop_loss": stop_loss, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "df": df}

analyzer = AdvancedCryptoAnalyzer()

# ---------- SENTIMENT ----------
class SentimentAnalyzer:
    def __init__(self):
        self.fear_greed_url = "https://api.alternative.me/fng/"

    async def get_fear_greed_index(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.fear_greed_url, timeout=10) as response:
                    data = await response.json()
                    return int(data['data'][0]['value'])
        except:
            return 50

    def interpret_fear_greed(self, index):
        if index <= 25: return "😨 ترس شدید", "🟢 فرصت خرید"
        elif index <= 45: return "😰 ترس", "🟡 شرایط مناسب"
        elif index <= 55: return "😐 خنثی", "⚪ احتیاط"
        elif index <= 75: return "😊 طمع", "🟠 هشدار"
        else: return "🤩 طمع شدید", "🔴 ریسک بالا"

sentiment_analyzer = SentimentAnalyzer()

# ---------- VISUALIZATION ----------
class AdvancedVisualization:
    def generate_performance_chart(self, chat_id):
        try:
            performance_data = performance_log.get(str(chat_id), [])
            if not performance_data: return None
            dates = [datetime.fromisoformat(x['time']) for x in performance_data]
            pnls = [x['pnl'] for x in performance_data]
            cumulative = np.cumsum(pnls)
            plt.figure(figsize=(10, 6))
            plt.plot(dates, cumulative, 'b-', linewidth=2, label='سود/ضرر تجمعی')
            plt.fill_between(dates, cumulative, alpha=0.3)
            plt.title('📈 نمودار عملکرد معاملاتی')
            plt.xlabel('زمان'); plt.ylabel('سود/ضرر (USD)')
            plt.grid(True, alpha=0.3); plt.legend()
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0); plt.close()
            return buf
        except Exception as e:
            print(f"خطا در تولید نمودار: {e}")
            return None

viz = AdvancedVisualization()

# ---------- PORTFOLIO ----------
class PortfolioManager:
    def __init__(self):
        self.positions_file = "portfolio_positions.json"
        self.positions = load_json(self.positions_file)

    def add_position(self, chat_id, symbol, entry_price, size, direction, stop_loss=None, take_profit=None, leverage=1):
        key = str(chat_id)
        self.positions.setdefault(key, {})
        position_id = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.positions[key][position_id] = {
            'symbol': symbol.upper(),
            'entry_price': float(entry_price),
            'size': float(size),
            'direction': direction.upper(),
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'leverage': int(leverage),
            'status': 'open',
            'created_at': datetime.now().isoformat()
        }
        save_json(self.positions_file, self.positions)
        return position_id

    def close_position(self, chat_id, position_id, exit_price):
        key = str(chat_id)
        if key in self.positions and position_id in self.positions[key]:
            pos = self.positions[key][position_id]
            if pos['status'] != 'open': return None
            entry = pos['entry_price']; size = pos['size']; direction = pos['direction']; lev = pos.get('leverage', 1)
            pnl = ((exit_price - entry) if direction == 'LONG' else (entry - exit_price)) * size * lev
            pos.update({'status': 'closed', 'exit_price': float(exit_price), 'pnl': pnl, 'closed_at': datetime.now().isoformat()})
            save_json(self.positions_file, self.positions)
            log_trade(chat_id, {"symbol": pos['symbol'], "entry": entry, "exit": exit_price, "pnl": pnl,
                                "time": pos['closed_at'], "direction": direction, "leverage": lev})
            return pnl
        return None

    def get_portfolio_summary(self, chat_id):
        positions = self.positions.get(str(chat_id), {})
        if not positions: return "📭 هیچ پوزیشن بازی ندارید"
        open_positions = [p for p in positions.values() if p['status'] == 'open']
        if not open_positions: return "📭 هیچ پوزیشن بازی ندارید"
        summary = "📊 پورتفوی شما:\n\n"
        total_invested = 0; total_current = 0
        for p in open_positions:
            analysis = analyzer.analyze_crypto(p['symbol'])
            current_price = analysis['current_price'] if analysis else p['entry_price']
            pnl = ((current_price - p['entry_price']) if p['direction']=='LONG' else (p['entry_price'] - current_price)) * p['size'] * p.get('leverage',1)
            invested = p['entry_price'] * p['size']
            pnl_percent = (pnl / invested * 100) if invested > 0 else 0
            total_invested += invested; total_current += invested + pnl
            summary += f"• {p['symbol']} {p['direction']}: {pnl:+.2f} USD ({pnl_percent:+.1f}%)\n"
        total_pnl = total_current - total_invested
        summary += f"\n💰 سرمایه‌گذاری شده: ${total_invested:,.2f}\n💵 ارزش فعلی: ${total_current:,.2f}\n📈 سود/زیان کل: {total_pnl:+.2f} USD"
        return summary

portfolio_manager = PortfolioManager()

# ---------- TRADE PLAN ----------
def build_trade_plan(analysis, balance_usd=1000, risk_per_trade=0.02):
    price = analysis['current_price']
    stop_loss = analysis['stop_loss']
    risk_amount = balance_usd * risk_per_trade
    sl_dist = abs(price - stop_loss) if price != stop_loss else 0
    position_size = (risk_amount / sl_dist) if sl_dist > 0 else 0
    supports = analysis['levels']['support'] or [round(price*0.98,2)]
    plan = f"""
🎯 پلن معاملاتی {analysis['symbol']}:

💰 قیمت فعلی: ${price:,.2f}
🎯 نقاط ورود:
  • ورود اول: ${price:,.2f}
  • ورود دوم: ${supports[0]:,.2f}

✅ اهداف قیمتی:"""
    if analysis['targets']:
        for i, target in enumerate(analysis['targets'][:3], 1):
            profit_percent = ((target / price) - 1) * 100
            plan += f"\n  • هدف {i}: ${target:,.2f} ({profit_percent:+.1f}%)"
    else:
        plan += "\n  • هدف مشخصی وجود ندارد"
    rr_text = "نامشخص"
    if analysis['targets']:
        profit = analysis['targets'][0] - price; loss = price - stop_loss
        if loss > 0: rr_text = f"{(profit/loss):.1f}"
    plan += f"""
🛑 استاپ لاس: ${stop_loss:,.2f}
📊 اندازه پوزیشن: ~{position_size:.4f} واحد
🧮 R/R: {rr_text}
💼 ریسک هر معامله: {risk_per_trade*100:.1f}%
"""
    return plan

# ---------- TELEGRAM UTILS ----------
def send_message_ctx(context, chat_id, text):
    try: return context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e: print(f"ERR send: {e}")

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["/BTC", "/ETH", "/SOL", "/BNB"],
        ["/تاپ_۱۰", "/تاپ_۲۰", "/تاپ_۵۰"],
        ["/تاپ_۱۰۰", "/تاپ_۲۰۰", "/اسکن_بازار"],
        ["/پورتفوی_من", "/احساسات_بازار BTC", "/عملکرد_من"],
        ["/prefs_balance 2000", "/prefs_risk 0.03", "/help"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    welcome_text = "🤖 بات تحلیل‌گر حرفه‌ای آماده است. یک نماد را انتخاب کنید یا از منو استفاده کنید."
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    buttons = [
        [InlineKeyboardButton("💱 صرافی تبدیل", url="https://tabdeal.org/markets")]
    ]
    await update.message.reply_text("برای معامله سریع:", reply_markup=InlineKeyboardMarkup(buttons))

async def helpcommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 راهنما
/start - شروع
/اسکن_بازار - اسکن Top symbols و فرصت‌ها
/تاپ_۱۰, /تاپ_۲۰, /تاپ_۵۰, /تاپ_۱۰۰, /تاپ_۲۰۰ - تاپ‌لیست
/prefs_balance 2000 - تنظیم بالانس
/prefs_risk 0.03 - تنظیم ریسک
/پورتفوی_من - خلاصه پورتفوی + نمودار عملکرد
/احساسات_بازار BTC - تحلیل احساسات
/عملکرد_من - خلاصه عملکرد معاملات
نمادها را مستقیم هم بفرست (مثل BTC یا ETH)
"""
    await update.message.reply_text(help_text)

async def analyzecrypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.replace('/', '').strip().upper()
    if not symbol or not symbol.isalnum(): return await update.message.reply_text("❌ نماد نامعتبر.")
    await update.message.reply_text(f"🔍 در حال تحلیل {symbol}...")
    analysis = analyzer.analyze_crypto(symbol)
    if not analysis: return await update.message.reply_text("❌ خطا در دریافت داده‌ها یا نماد نامعتبر")
    confidence = max(min(analysis['signal_data']['confidence'], 5), -5)
    signals = analysis['signal_data']['signals'][:5] or ["سیگنالی ثبت نشد"]
    current_price = analysis['current_price']
    supports = analysis['levels']['support'] or [round(current_price * 0.98, 2)]
    resistances = analysis['levels']['resistance'] or [round(current_price * 1.02, 2)]
    report = f"""
📊 تحلیل پیشرفته {analysis['symbol']}
⏰ زمان: {analysis['timestamp']}

💰 قیمت فعلی: ${current_price:,.2f}
🎯 سیگنال اصلی: {analysis['signal_data']['overall_signal']}
📈 سطح اطمینان: {confidence}/5

📋 سیگنال‌ها:
""" + "\n".join([f"• {sig}" for sig in signals]) + "\n\n" + \
    "🛡️ حمایت‌ها:\n" + "\n".join([f"• ${lvl:,.2f}" for lvl in supports[:3]]) + "\n\n" + \
    "🎯 مقاومت‌ها:\n" + "\n".join([f"• ${lvl:,.2f}" for lvl in resistances[:3]])
    await update.message.reply_text(report)

    # Dashboard buttons + Tabdeal
    buttons = [
        [InlineKeyboardButton("🎯 پلن سودگیری", callback_data=f"TP|{analysis['symbol']}")],
        [InlineKeyboardButton("🔔 هشدار قیمت", callback_data=f"ALERT_PRICE|{analysis['symbol']}"),
         InlineKeyboardButton("📈 نمودار عملکرد", callback_data=f"CHART|{analysis['symbol']}")],
        [InlineKeyboardButton("💱 معامله در تبدیل", url=f"https://tabdeal.org/markets/{analysis['symbol'].lower()}-usdt")]
    ]
    await update.message.reply_text("گزینه‌های سریع:", reply_markup=InlineKeyboardMarkup(buttons))

    # Check alerts
    df_raw = analyzer.get_ohlc_data(symbol, days=90)
    df_ind = analyzer.calculate_indicators(df_raw) if df_raw is not None else None
    indicators = {"rsi": df_ind['rsi_14'].iloc[-1] if df_ind is not None else None,
                  "macd": df_ind['macd'].iloc[-1] if df_ind is not None else None,
                  "macd_signal": df_ind['macd_signal'].iloc[-1] if df_ind is not None else None}
    alerts.check_alerts(update.effective_chat.id, symbol, current_price, indicators, df_ind,
                        send_fn=lambda chat_id, txt: send_message_ctx(context, chat_id, txt))

    prefs = get_user_prefs(update.effective_chat.id)
    final_notes = f"👤 پروفایل شما: بالانس ${prefs['balance']:,.2f} | ریسک {prefs['risk']*100:.0f}%"
    await update.message.reply_text(final_notes)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data; chat_id = update.effective_chat.id
    if data.startswith("TP|"):
        symbol = data.split("|")[1]
        analysis = analyzer.analyze_crypto(symbol)
        if not analysis: return await query.edit_message_text("❌ خطا در پلن سودگیری")
        prefs = get_user_prefs(chat_id)
        plan = build_trade_plan(analysis, balance_usd=prefs["balance"], risk_per_trade=prefs["risk"])
        await query.edit_message_text(f"📈 پلن سودگیری {analysis['symbol']}:\n\n{plan}\n\n⚠️ آموزشی است؛ مسئولیت معاملات با شماست.")
        if analysis['signal_data']['confidence'] >= 2:
            price = analysis['current_price']
            risk_usd = prefs["balance"] * prefs["risk"]; sl_dist = abs(price - analysis['stop_loss'])
            size = (risk_usd / sl_dist) if sl_dist > 0 else 0
            alerts.start_tracking(chat_id, symbol, entry=price, size=size, direction="LONG", trailing_stop=None)
            await send_message_ctx(context, chat_id, f"📡 رصد تریلینگ {symbol} آغاز شد (LONG).")
    elif data.startswith("ALERT_PRICE|"):
        symbol = data.split("|")[1]
        analysis = analyzer.analyze_crypto(symbol)
        if not analysis: return await query.edit_message_text("❌ خطا در هشدار")
        price = analysis['current_price']
        target_level = analysis['levels']['resistance'][0] if analysis['signal_data']['confidence'] >= 2 and analysis['levels']['resistance'] else round(price*1.02, 2)
        alerts.add_price_alert(chat_id, symbol, target_level, "price_above")
        await query.edit_message_text(f"🔔 هشدار قیمت ثبت شد: {symbol} سطح ${target_level:,.2f}")
    elif data.startswith("CHART|"):
        # ارسال نمودار عملکرد کاربر
        buf = viz.generate_performance_chart(chat_id)
        if buf:
            await context.bot.send_photo(chat_id=chat_id, photo=buf, caption="📊 نمودار عملکرد معاملاتی شما")
        else:
            await query.edit_message_text("⚠️ هنوز نموداری برای عملکرد شما ثبت نشده.")

async def marketscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 اسکن بازار در حال اجراست...")
    # Simple scan example on a fixed list
    universe = ['BTC','ETH','BNB','SOL','ADA','XRP','DOT','AVAX','DOGE','LTC','LINK','MATIC']
    results = []
    for s in universe:
        try:
            an = analyzer.analyze_crypto(s)
            if not an: continue
            rr = None
            if an['targets']:
                profit = an['targets'][0] - an['current_price']
                loss = an['current_price'] - an['stop_loss']
                rr = (profit/loss) if loss > 0 else None
            score = an['signal_data']['confidence']
            results.append({'symbol': s, 'score': score, 'rr': rr or 0, 'price': an['current_price'], 'signal': an['signal_data']['overall_signal']})
        except Exception as e:
            print("scan err", e)
    if not results: return await update.message.reply_text("⚠️ فرصت قابل ارائه یافت نشد.")
    results = sorted(results, key=lambda x: (x['score'], x['rr']), reverse=True)[:20]
    msg = "🏆 فرصت‌ها:\n\n" + "\n".join([f"• {r['symbol']} | امتیاز: {r['score']:.1f} | R/R: {r['rr']:.2f} | قیمت: ${r['price']:,.2f} | {r['signal']}" for r in results])
    await update.message.reply_text(msg)

async def top_scan_generic(update: Update, context: ContextTypes.DEFAULT_TYPE, n: int):
    await update.message.reply_text(f"🔍 اسکن تاپ {n} در حال اجراست...")
    # For simplicity reuse marketscan universe slice
    universe = ['BTC','ETH','BNB','SOL','ADA','XRP','DOT','AVAX','DOGE','LTC','LINK','MATIC','NEAR','ATOM','ICP','BCH','TON','TRX'][:n]
    results = []
    for s in universe:
        try:
            an = analyzer.analyze_crypto(s)
            if not an: continue
            rr = None
            if an['targets']:
                profit = an['targets'][0] - an['current_price']
                loss = an['current_price'] - an['stop_loss']
                rr = (profit/loss) if loss > 0 else None
            score = an['signal_data']['confidence']
            results.append({'symbol': s, 'score': score, 'rr': rr or 0, 'price': an['current_price'], 'signal': an['signal_data']['overall_signal']})
        except Exception as e:
            print("top scan err", e)
    if not results: return await update.message.reply_text("⚠️ فرصت قابل ارائه یافت نشد.")
    results = sorted(results, key=lambda x: (x['score'], x['rr']), reverse=True)[:20]
    msg = f"🏆 فرصت‌های کم‌ریسک در تاپ {n}:\n\n" + "\n".join([f"• {r['symbol']} | امتیاز: {r['score']:.1f} | R/R: {r['rr']:.2f} | قیمت: ${r['price']:,.2f} | {r['signal']}" for r in results])
    await update.message.reply_text(msg)

async def top10(update: Update, context: ContextTypes.DEFAULT_TYPE):  return await top_scan_generic(update, context, 10)
async def top20(update: Update, context: ContextTypes.DEFAULT_TYPE):  return await top_scan_generic(update, context, 20)
async def top50(update: Update, context: ContextTypes.DEFAULT_TYPE):  return await top_scan_generic(update, context, 50)
async def top100(update: Update, context: ContextTypes.DEFAULT_TYPE): return await top_scan_generic(update, context, 100)
async def top200(update: Update, context: ContextTypes.DEFAULT_TYPE): return await top_scan_generic(update, context, 200)

async def alert_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.replace('/هشدار', '').strip().split()
        symbol = parts[0].upper(); level = float(parts[1])
        alerts.add_price_alert(update.effective_chat.id, symbol, level, "price_above")
        await update.message.reply_text(f"🔔 هشدار قیمت ثبت شد: {symbol} سطح ${level:,.2f}")
    except Exception:
        await update.message.reply_text("❌ فرمت: /هشدار BTC 35000")

async def alert_indicator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.replace('/هشدار_اندیکاتور', '').strip().split()
        symbol = parts[0].upper(); kind = parts[1].upper()
        valid = {'MACD_CROSS_UP','MACD_CROSS_DOWN','RSI_OVERBOUGHT','RSI_OVERSOLD'}
        if kind not in valid: return await update.message.reply_text("❌ مجاز: MACD_CROSS_UP, MACD_CROSS_DOWN, RSI_OVERBOUGHT, RSI_OVERSOLD")
        alerts.add_indicator_alert(update.effective_chat.id, symbol, kind)
        await update.message.reply_text(f"🔔 هشدار ثبت شد: {symbol} - {kind}")
    except Exception:
        await update.message.reply_text("❌ فرمت: /هشدار_اندیکاتور BTC MACD_CROSS_UP")

async def prefs_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.strip().split()
        if len(parts) != 2: return await update.message.reply_text("❌ فرمت: /prefs_balance 2000")
        bal = float(parts[1]); prefs = set_user_prefs(update.effective_chat.id, balance=bal)
        await update.message.reply_text(f"✅ بالانس ذخیره شد: ${prefs['balance']:,.2f}")
    except Exception:
        await update.message.reply_text("❌ مقدار نامعتبر. نمونه: /prefs_balance 2000")

async def prefs_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.strip().split()
        if len(parts) != 2: return await update.message.reply_text("❌ فرمت: /prefs_risk 0.03")
        risk = float(parts[1])
        if risk <= 0 or risk > 0.1: return await update.message.reply_text("❌ ریسک بین 0.001 تا 0.1 باشد.")
        prefs = set_user_prefs(update.effective_chat.id, risk=risk)
        await update.message.reply_text(f"✅ ریسک ذخیره شد: {prefs['risk']*100:.0f}%")
    except Exception:
        await update.message.reply_text("❌ مقدار نامعتبر. نمونه: /prefs_risk 0.03")

async def performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summary = summarize_performance(update.effective_chat.id)
    await update.message.reply_text(summary)

async def portfolio_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    summary = portfolio_manager.get_portfolio_summary(chat_id)
    await update.message.reply_text(summary)
    chart_buffer = viz.generate_performance_chart(chat_id)
    if chart_buffer:
        await context.bot.send_photo(chat_id=chat_id, photo=chart_buffer, caption="📊 نمودار عملکرد معاملاتی شما")

async def market_sentiment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else 'BTC'
    await update.message.reply_text("🔍 در حال تحلیل احساسات بازار...")
    fear_greed = await sentiment_analyzer.get_fear_greed_index()
    mood, tip = sentiment_analyzer.interpret_fear_greed(fear_greed)
    analysis = analyzer.analyze_crypto(symbol)
    price = analysis['current_price'] if analysis else None
    sentiment_report = f"""
🎭 تحلیل احساسات بازار
📊 شاخص ترس و طمع: {fear_greed}/100 → {mood}
💡 تفسیر: {tip}
{f'💰 قیمت {symbol}: ${price:,.2f}' if price else ''}
"""
    await update.message.reply_text(sentiment_report)

async def handledirectmessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.upper().strip()
    if text.isalnum() and 2 <= len(text) <= 6:
        return await analyzecrypto_command(update, context)
    else:
        return await update.message.reply_text("❌ دستور نامعتبر. از /help راهنمایی بگیرید یا از منو استفاده کنید.")

# ---------- MAIN ----------
def main():
    print("🤖 در حال راه‌اندازی بات تحلیل‌گر پیشرفته...")
    if not BOT_TOKEN:
        print("❌ توکن بات تنظیم نشده!"); return
    application = Application.builder().token(BOT_TOKEN).build()

    # عمومی
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", helpcommand))
    application.add_handler(CallbackQueryHandler(on_callback))

    # تحلیل مستقیم نمادها
    for symbol in ['BTC','ETH','SOL','BNB','XRP','ADA','DOT','AVAX','DOGE','LTC','LINK','MATIC','NEAR','ATOM','ICP','BCH','TON','TRX']:
        application.add_handler(CommandHandler(symbol, analyzecrypto_command))

    # اسکن‌ها
    application.add_handler(CommandHandler("اسکن_بازار", marketscan))
    application.add_handler(CommandHandler("تاپ_۱۰", top10))
    application.add_handler(CommandHandler("تاپ_۲۰", top20))
    application.add_handler(CommandHandler("تاپ_۵۰", top50))
    application.add_handler(CommandHandler("تاپ_۱۰۰", top100))
    application.add_handler(CommandHandler("تاپ_۲۰۰", top200))

    # هشدارها و تنظیمات
    application.add_handler(CommandHandler("هشدار", alert_price))
    application.add_handler(CommandHandler("هشدار_اندیکاتور", alert_indicator))
    application.add_handler(CommandHandler("prefs_balance", prefs_balance))
    application.add_handler(CommandHandler("prefs_risk", prefs_risk))

    # عملکرد و پورتفوی و احساسات
    application.add_handler(CommandHandler("عملکرد_من", performance))
    application.add_handler(CommandHandler("پورتفوی_من", portfolio_summary))
    application.add_handler(CommandHandler("احساسات_بازار", market_sentiment))

    # پیام‌های مستقیم
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handledirectmessage))

    print("✅ بات تحلیل‌گر پیشرفته فعال شد!")
    application.run_polling()

if __name__ == "__main__":
    main()

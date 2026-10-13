# -*- coding: utf-8 -*-
"""
ربات تلگرام سیگنال‌دهی USD/JPY - نسخه یکپارچه
USD/JPY Signal Bot - All-in-One Version

نحوه اجرا:
1. این فایل رو با نام bot.py ذخیره کن
2. فایل .env درست کن (راهنما در پایین)
3. python bot.py
"""
import os
import sys
import json
import logging
import asyncio
import time
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

# ============================================================
# نصب خودکار وابستگی‌ها (با مدیریت خطا)
# ============================================================
def install_dependencies():
    """نصب خودکار وابستگی‌ها"""
    print("=" * 50)
    print("📦 نصب وابستگی‌ها...")
    print("=" * 50)
    deps = [
        "yfinance", "pandas", "numpy",
        "python-telegram-bot", "APScheduler",
        "requests", "python-dotenv", "tabulate"
    ]
    for dep in deps:
        print(f"  نصب: {dep}...")
        ret = os.system(f'pip install {dep} --quiet')
        if ret != 0:
            print(f"  ⚠️ خطا در نصب {dep}، تلاش با --user...")
            os.system(f'pip install {dep} --user --quiet')
    print("✅ نصب کامل شد!\n")

install_dependencies()

# ============================================================
# Imports
# ============================================================
try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
    from dotenv import load_dotenv
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
except ImportError as e:
    print(f"\n❌ خطا در ایمپورت: {e}")
    print("لطفاً دستور زیر را اجرا کنید:")
    print("pip install yfinance pandas numpy pandas-ta python-telegram-bot APScheduler requests python-dotenv tabulate")
    sys.exit(1)

load_dotenv()

# ============================================================
# Configuration
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_IDS = [int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()]

SYMBOL = "JPY=X"
SYMBOL_DISPLAY = "USD/JPY"
PRIMARY_TIMEFRAME = "15m"
SECONDARY_TIMEFRAME = "1h"
TREND_TIMEFRAME = "4h"
DAILY_TIMEFRAME = "1d"
CANDLE_COUNT = 300

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
DAILY_MAX_LOSS = float(os.getenv("DAILY_MAX_LOSS", "0.03"))
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "3"))
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0
TRAILING_STOP_ACTIVATION = 1.0
TRAILING_STOP_DISTANCE = 0.5

# Indicator parameters
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_NEUTRAL_LOW = 40
RSI_NEUTRAL_HIGH = 60
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
EMA_FAST = 21
EMA_MID = 50
EMA_SLOW = 200
BB_PERIOD = 20
BB_STD = 2.0
STOCH_PERIOD = 14
STOCH_SMOOTH_K = 3
STOCH_SMOOTH_D = 3
STOCH_OVERSOLD = 20
STOCH_OVERBOUGHT = 80
ATR_PERIOD = 14
ICHIMOKU_TENKAN = 9
ICHIMOKU_KIJUN = 26
ICHIMOKU_SENKOU_B = 52
ICHIMOKU_DISPLACEMENT = 26
FIB_LEVELS = [0.236, 0.382, 0.5, 0.618, 0.786]
VOLUME_MA_PERIOD = 20
VOLUME_SPIKE_THRESHOLD = 1.5
MIN_SIGNAL_SCORE = 6
STRONG_SIGNAL_SCORE = 8
SIGNAL_CHECK_INTERVAL = 5
DAILY_RESET_HOUR = 0
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
AI_ANALYSIS_ENABLED = bool(ZAI_API_KEY)
AI_MODEL = "glm-4.5"
ECON_CALENDAR_URL = "https://nfs.faireconomy.media/cc_calendar_thisweek.json"
HIGH_IMPACT_ONLY = True
CHECK_NEWS_BEFORE_SIGNAL = True
NEWS_BLACKOUT_MINUTES = 30
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SIGNALS_DB = os.path.join(DATA_DIR, "signals.json")
TRADES_DB = os.path.join(DATA_DIR, "trades.json")
BACKTEST_RESULTS = os.path.join(DATA_DIR, "backtest_results.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")

os.makedirs(DATA_DIR, exist_ok=True)

INDICATOR_WEIGHTS = {
    "trend": 2.0, "rsi": 1.0, "macd": 1.5, "bollinger": 1.0,
    "stochastic": 1.0, "ichimoku": 1.5, "volume": 1.0, "price_action": 1.0,
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# ============================================================
# Data Fetcher
# ============================================================
class DataFetcher:
    def __init__(self):
        self.ticker = yf.Ticker(SYMBOL)
        self._cache = {}
        self._cache_time = {}
        self._cache_ttl = 60

    def _is_cache_valid(self, key):
        if key not in self._cache_time:
            return False
        return (time.time() - self._cache_time[key]) < self._cache_ttl

    def fetch_ohlc(self, timeframe=PRIMARY_TIMEFRAME, count=CANDLE_COUNT):
        cache_key = f"{timeframe}_{count}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key].copy()
        try:
            period = self._calculate_period(timeframe, count)
            df = self.ticker.history(period=period, interval=timeframe, auto_adjust=False, prepost=False)
            if df.empty:
                df = yf.download(SYMBOL, period=period, interval=timeframe, progress=False, auto_adjust=False)
            if df.empty:
                raise ValueError("داده خالی")
            df.columns = [str(c).lower().replace(' ', '_') for c in df.columns]
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    df[col] = 0 if col == 'volume' else df[col].iloc[:, 0] if hasattr(df[col], 'iloc') else 0
            df = df.dropna(subset=['open', 'high', 'low', 'close'])
            if len(df) > count:
                df = df.tail(count)
            self._cache[cache_key] = df.copy()
            self._cache_time[cache_key] = time.time()
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {timeframe}: {e}")
            raise

    def _calculate_period(self, timeframe, count):
        if timeframe == "15m":
            return "60d"
        elif timeframe in ("1h", "60m"):
            return "730d"
        elif timeframe == "4h":
            return "60d"
        elif timeframe == "1d":
            return "2y"
        elif timeframe == "1m":
            return "5d"
        return "60d"

    def get_current_price(self):
        df = self.fetch_ohlc(PRIMARY_TIMEFRAME, count=5)
        return float(df['close'].iloc[-1])

    def get_market_info(self):
        try:
            df = self.fetch_ohlc(DAILY_TIMEFRAME, count=5)
            current = float(df['close'].iloc[-1])
            prev_close = float(df['close'].iloc[-2]) if len(df) > 1 else current
            change = current - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            return {
                "symbol": SYMBOL_DISPLAY, "current_price": current, "prev_close": prev_close,
                "change": change, "change_pct": change_pct,
                "high_24h": float(df['high'].iloc[-1]), "low_24h": float(df['low'].iloc[-1]),
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        except Exception as e:
            logger.error(f"خطا در دریافت اطلاعات بازار: {e}")
            return {}


# ============================================================
# Indicators Engine
# ============================================================
class IndicatorsEngine:
    def ema(self, s, p): return s.ewm(span=p, adjust=False).mean()
    def sma(self, s, p): return s.rolling(window=p).mean()

    def calculate_moving_averages(self, df):
        df = df.copy()
        df['ema_21'] = self.ema(df['close'], EMA_FAST)
        df['ema_50'] = self.ema(df['close'], EMA_MID)
        df['ema_200'] = self.ema(df['close'], EMA_SLOW)
        df['sma_20'] = self.sma(df['close'], 20)
        return df

    def rsi(self, s, p=RSI_PERIOD):
        delta = s.diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/p, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/p, adjust=False).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def macd(self, s, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
        ema_fast = self.ema(s, fast)
        ema_slow = self.ema(s, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self.ema(macd_line, signal)
        hist = macd_line - signal_line
        return macd_line, signal_line, hist

    def bollinger_bands(self, s, period=BB_PERIOD, std_dev=BB_STD):
        middle = self.sma(s, period)
        std = s.rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        bandwidth = (upper - lower) / middle
        return middle, upper, lower, bandwidth

    def stochastic(self, df, period=STOCH_PERIOD):
        low_min = df['low'].rolling(window=period).min()
        high_max = df['high'].rolling(window=period).max()
        k_fast = 100 * ((df['close'] - low_min) / (high_max - low_min))
        k = k_fast.rolling(window=3).mean()
        d = k.rolling(window=3).mean()
        return k, d

    def atr(self, df, period=ATR_PERIOD):
        hl = df['high'] - df['low']
        hc = (df['high'] - df['close'].shift()).abs()
        lc = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean()

    def ichimoku_cloud(self, df):
        df = df.copy()
        ph9 = df['high'].rolling(window=ICHIMOKU_TENKAN).max()
        pl9 = df['low'].rolling(window=ICHIMOKU_TENKAN).min()
        df['tenkan_sen'] = (ph9 + pl9) / 2
        ph26 = df['high'].rolling(window=ICHIMOKU_KIJUN).max()
        pl26 = df['low'].rolling(window=ICHIMOKU_KIJUN).min()
        df['kijun_sen'] = (ph26 + pl26) / 2
        df['senkou_span_a'] = ((df['tenkan_sen'] + df['kijun_sen']) / 2).shift(ICHIMOKU_DISPLACEMENT)
        ph52 = df['high'].rolling(window=ICHIMOKU_SENKOU_B).max()
        pl52 = df['low'].rolling(window=ICHIMOKU_SENKOU_B).min()
        df['senkou_span_b'] = ((ph52 + pl52) / 2).shift(ICHIMOKU_DISPLACEMENT)
        df['chikou_span'] = df['close'].shift(-ICHIMOKU_DISPLACEMENT)
        return df

    def calculate_all(self, df):
        df = df.copy()
        df = self.calculate_moving_averages(df)
        df['rsi'] = self.rsi(df['close'])
        m, s, h = self.macd(df['close'])
        df['macd'], df['macd_signal'], df['macd_histogram'] = m, s, h
        bb_m, bb_u, bb_l, bb_w = self.bollinger_bands(df['close'])
        df['bb_middle'], df['bb_upper'], df['bb_lower'], df['bb_width'] = bb_m, bb_u, bb_l, bb_w
        k, d = self.stochastic(df)
        df['stoch_k'], df['stoch_d'] = k, d
        df['atr'] = self.atr(df)
        df = self.ichimoku_cloud(df)
        if 'volume' in df.columns and df['volume'].sum() > 0:
            df['volume_ma'] = self.sma(df['volume'], VOLUME_MA_PERIOD)
            df['volume_ratio'] = df['volume'] / df['volume_ma']
        else:
            df['volume_ma'], df['volume_ratio'] = 0, 1.0
        return df

    def get_trend_direction(self, df):
        if len(df) < EMA_SLOW or 'ema_200' not in df.columns:
            return {"direction": "neutral", "strength": 0, "description": "داده کافی نیست"}
        price = df['close'].iloc[-1]
        e21, e50, e200 = df['ema_21'].iloc[-1], df['ema_50'].iloc[-1], df['ema_200'].iloc[-1]
        bull = bear = 0
        if price > e200: bull += 1
        else: bear += 1
        if e21 > e50: bull += 1
        else: bear += 1
        if e50 > e200: bull += 1
        else: bear += 1
        slope = df['ema_21'].iloc[-1] - df['ema_21'].iloc[-5]
        if slope > 0: bull += 1
        else: bear += 1
        if bull > bear:
            return {"direction": "bullish", "strength": (bull/4)*100, "description": f"روند صعودی ({(bull/4)*100:.0f}%)"}
        elif bear > bull:
            return {"direction": "bearish", "strength": (bear/4)*100, "description": f"روند نزولی ({(bear/4)*100:.0f}%)"}
        return {"direction": "neutral", "strength": 50, "description": "روند خنثی"}

    def get_rsi_signal(self, df):
        if 'rsi' not in df.columns:
            return {"signal": "neutral", "score": 50, "description": "RSI محاسبه نشده"}
        rsi = df['rsi'].iloc[-1]
        prev = df['rsi'].iloc[-2] if len(df) > 1 else rsi
        if pd.isna(rsi):
            return {"signal": "neutral", "score": 50, "description": "RSI نامعتبر"}
        trend = "rising" if rsi > prev else "falling"
        if rsi < RSI_OVERSOLD:
            return {"signal": "bullish", "score": 80, "description": f"RSI: {rsi:.1f} - اشباع فروش (صعودی)", "value": rsi, "trend": trend}
        if rsi > RSI_OVERBOUGHT:
            return {"signal": "bearish", "score": 80, "description": f"RSI: {rsi:.1f} - اشباع خرید (نزولی)", "value": rsi, "trend": trend}
        if RSI_NEUTRAL_LOW < rsi < RSI_NEUTRAL_HIGH:
            return {"signal": "neutral", "score": 50, "description": f"RSI: {rsi:.1f} - خنثی", "value": rsi, "trend": trend}
        if rsi > RSI_NEUTRAL_HIGH and trend == "rising":
            return {"signal": "bullish", "score": 65, "description": f"RSI: {rsi:.1f} - رو به بالا (صعودی)", "value": rsi, "trend": trend}
        if rsi < RSI_NEUTRAL_LOW and trend == "falling":
            return {"signal": "bearish", "score": 65, "description": f"RSI: {rsi:.1f} - رو به پایین (نزولی)", "value": rsi, "trend": trend}
        return {"signal": "neutral", "score": 50, "description": f"RSI: {rsi:.1f}", "value": rsi, "trend": trend}

    def get_macd_signal(self, df):
        if 'macd' not in df.columns:
            return {"signal": "neutral", "score": 50, "description": "MACD محاسبه نشده"}
        mv, sv, h = df['macd'].iloc[-1], df['macd_signal'].iloc[-1], df['macd_histogram'].iloc[-1]
        ph = df['macd_histogram'].iloc[-2] if len(df) > 1 else h
        if pd.isna(mv) or pd.isna(sv):
            return {"signal": "neutral", "score": 50, "description": "MACD نامعتبر"}
        bull = bear = 0
        if mv > sv: bull += 1
        else: bear += 1
        if h > 0: bull += 1
        else: bear += 1
        if h > ph: bull += 1
        else: bear += 1
        if mv > 0: bull += 1
        else: bear += 1
        if bull >= 3:
            return {"signal": "bullish", "score": 60+(bull-3)*10, "description": f"MACD: صعودی ({bull}/4)"}
        if bear >= 3:
            return {"signal": "bearish", "score": 60+(bear-3)*10, "description": f"MACD: نزولی ({bear}/4)"}
        return {"signal": "neutral", "score": 50, "description": "MACD: خنثی"}

    def get_bollinger_signal(self, df):
        if 'bb_upper' not in df.columns:
            return {"signal": "neutral", "score": 50, "description": "Bollinger محاسبه نشده"}
        price = df['close'].iloc[-1]
        u, l, m, w = df['bb_upper'].iloc[-1], df['bb_lower'].iloc[-1], df['bb_middle'].iloc[-1], df['bb_width'].iloc[-1]
        if pd.isna(u) or pd.isna(l):
            return {"signal": "neutral", "score": 50, "description": "Bollinger نامعتبر"}
        if price <= l:
            return {"signal": "bullish", "score": 75, "description": "Bollinger: لمس باند پایین"}
        if price >= u:
            return {"signal": "bearish", "score": 75, "description": "Bollinger: لمس باند بالایی"}
        if price > m:
            return {"signal": "bullish", "score": 60, "description": "Bollinger: بالای خط میانی"}
        return {"signal": "bearish", "score": 60, "description": "Bollinger: زیر خط میانی"}

    def get_stochastic_signal(self, df):
        if 'stoch_k' not in df.columns:
            return {"signal": "neutral", "score": 50, "description": "Stochastic محاسبه نشده"}
        k = df['stoch_k'].iloc[-1]
        d = df['stoch_d'].iloc[-1]
        pk = df['stoch_k'].iloc[-2] if len(df) > 1 else k
        if pd.isna(k) or pd.isna(d):
            return {"signal": "neutral", "score": 50, "description": "Stochastic نامعتبر"}
        if k < STOCH_OVERSOLD and k > pk:
            return {"signal": "bullish", "score": 75, "description": f"Stochastic: اشباع فروش + واژگون"}
        if k > STOCH_OVERBOUGHT and k < pk:
            return {"signal": "bearish", "score": 75, "description": f"Stochastic: اشباع خرید + واژگون"}
        if k > d and k > pk:
            return {"signal": "bullish", "score": 65, "description": "Stochastic: صعودی"}
        if k < d and k < pk:
            return {"signal": "bearish", "score": 65, "description": "Stochastic: نزولی"}
        return {"signal": "neutral", "score": 50, "description": f"Stochastic: خنثی (K={k:.1f})"}

    def get_ichimoku_signal(self, df):
        if 'tenkan_sen' not in df.columns:
            return {"signal": "neutral", "score": 0, "description": "ایچیموکو: داده نیست"}
        if len(df) < ICHIMOKU_SENKOU_B + ICHIMOKU_DISPLACEMENT:
            return {"signal": "neutral", "score": 0, "description": "ایچیموکو: داده کافی نیست"}
        price = df['close'].iloc[-1]
        t, k = df['tenkan_sen'].iloc[-1], df['kijun_sen'].iloc[-1]
        sa, sb = df['senkou_span_a'].iloc[-1], df['senkou_span_b'].iloc[-1]
        bull = bear = 0
        ct = max(sa, sb) if not pd.isna(sa) and not pd.isna(sb) else None
        cb = min(sa, sb) if not pd.isna(sa) and not pd.isna(sb) else None
        if ct and cb:
            if price > ct: bull += 1
            elif price < cb: bear += 1
        if t > k: bull += 1
        elif t < k: bear += 1
        if not pd.isna(sa) and not pd.isna(sb):
            if sa > sb: bull += 1
            else: bear += 1
        if price > k: bull += 1
        else: bear += 1
        if bull >= 3:
            return {"signal": "bullish", "score": (bull/4)*100, "description": f"ایچیموکو: صعودی ({bull}/4)"}
        if bear >= 3:
            return {"signal": "bearish", "score": (bear/4)*100, "description": f"ایچیموکو: نزولی ({bear}/4)"}
        return {"signal": "neutral", "score": 50, "description": "ایچیموکو: خنثی"}

    def volume_analysis(self, df):
        if 'volume' not in df.columns or df['volume'].sum() == 0:
            return {"available": False, "description": "داده حجم در دسترس نیست", "signal": "neutral"}
        df = df.copy()
        df['volume_ma'] = self.sma(df['volume'], VOLUME_MA_PERIOD)
        cv = df['volume'].iloc[-1]
        av = df['volume_ma'].iloc[-1] if not pd.isna(df['volume_ma'].iloc[-1]) else cv
        ratio = cv / av if av > 0 else 1.0
        spike = ratio > VOLUME_SPIKE_THRESHOLD
        sig = "bullish" if spike and df['close'].iloc[-1] > df['close'].iloc[-2] else "bearish" if spike else "neutral"
        return {"available": True, "current_volume": cv, "average_volume": av, "volume_ratio": ratio, "is_spike": spike, "signal": sig, "description": f"حجم: {ratio:.2f}x میانگین"}

    def detect_candlestick_patterns(self, df):
        patterns = []
        if len(df) < 3:
            return patterns
        c, p = df.iloc[-1], df.iloc[-2]
        body = abs(c['close'] - c['open'])
        us = c['high'] - max(c['close'], c['open'])
        ls = min(c['close'], c['open']) - c['low']
        tr = c['high'] - c['low']
        if tr == 0:
            return patterns
        if body < 0.1 * tr:
            patterns.append({"name": "Doji", "name_fa": "دوجی", "type": "neutral", "description": "الگوی دوجی - عدم قطعیت"})
        if ls > 2 * body and us < body and body > 0:
            patterns.append({"name": "Hammer", "name_fa": "چکش صعودی", "type": "bullish", "description": "الگوی چکش - بازگشت صعودی"})
        if us > 2 * body and ls < body and body > 0:
            patterns.append({"name": "Shooting Star", "name_fa": "ستاره دنباله‌دار", "type": "bearish", "description": "الگوی ستاره دنباله‌دار - بازگشت نزولی"})
        if p['close'] < p['open'] and c['close'] > c['open'] and c['close'] >= p['open'] and c['open'] <= p['close']:
            patterns.append({"name": "Bullish Engulfing", "name_fa": "پوشش صعودی", "type": "bullish", "description": "الگوی پوشش صعودی - سیگنال خرید"})
        if p['close'] > p['open'] and c['close'] < c['open'] and c['open'] >= p['close'] and c['close'] <= p['open']:
            patterns.append({"name": "Bearish Engulfing", "name_fa": "پوشش نزولی", "type": "bearish", "description": "الگوی پوشش نزولی - سیگنال فروش"})
        return patterns

    def detect_support_resistance(self, df, lookback=50, num_levels=3):
        recent = df.tail(lookback)
        cp = df['close'].iloc[-1]
        highs, lows = [], []
        for i in range(2, len(recent) - 2):
            if recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i+1]:
                highs.append(recent['high'].iloc[i])
            if recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i+1]:
                lows.append(recent['low'].iloc[i])
        return {"resistances": sorted([h for h in highs if h > cp])[:num_levels], "supports": sorted([l for l in lows if l < cp], reverse=True)[:num_levels], "current_price": cp}

    def fibonacci_retracement(self, df, lookback=100):
        recent = df.tail(lookback)
        h, l = recent['high'].max(), recent['low'].min()
        diff = h - l
        cur = df['close'].iloc[-1]
        levels = {f"{lv*100:.1f}%": h - (diff * lv) for lv in FIB_LEVELS}
        closest, cd = None, float('inf')
        for n, v in levels.items():
            d = abs(cur - v)
            if d < cd: cd, closest = d, n
        return {"high": h, "low": l, "levels": levels, "closest_level": closest, "closest_value": levels.get(closest), "current_price": cur}


# ============================================================
# Signal
# ============================================================
class Signal:
    def __init__(self):
        self.symbol = SYMBOL_DISPLAY
        self.direction = "WAIT"
        self.strength = 0
        self.score = 0
        self.max_score = 10
        self.entry_price = None
        self.stop_loss = None
        self.take_profit_1 = None
        self.take_profit_2 = None
        self.take_profit_3 = None
        self.risk_reward = 0
        self.timestamp = datetime.now()
        self.timeframe = PRIMARY_TIMEFRAME
        self.confirmed = False
        self.confidence = "low"
        self.bullish_pct = 50
        self.bearish_pct = 50
        self.indicators = {}
        self.patterns = []
        self.description = ""
        self.warnings = []

    def to_dict(self):
        return {
            "symbol": self.symbol, "direction": self.direction, "strength": self.strength,
            "score": self.score, "max_score": self.max_score, "entry_price": self.entry_price,
            "stop_loss": self.stop_loss, "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2, "take_profit_3": self.take_profit_3,
            "risk_reward": self.risk_reward, "timestamp": self.timestamp.isoformat(),
            "timeframe": self.timeframe, "confirmed": self.confirmed,
            "indicators": self.indicators, "patterns": self.patterns,
            "description": self.description, "warnings": self.warnings
        }

    def to_persian_message(self):
        if self.direction == "BUY":
            icon, dir_fa, arr = "🟢", "خرید", "▲"
        elif self.direction == "SELL":
            icon, dir_fa, arr = "🔴", "فروش", "▼"
        else:
            icon, dir_fa, arr = "⏳", "صبر", "●"

        # آیکون اطمینان
        conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(self.confidence, "⚪")

        msg = f"{icon} سیگنال {dir_fa} {self.symbol}\nجهت: {arr} {self.direction}\n{'='*30}\n\n"
        if self.entry_price:
            msg += f"💰 قیمت فعلی: {self.entry_price:.3f}\n"
        msg += f"📊 امتیاز: {self.score}/{self.max_score}\n"
        msg += f"💪 قدرت: {self.strength:.0f}%\n"
        msg += f"{conf_icon} اطمینان: {self.confidence}\n"
        msg += f"📈 صعودی: {self.bullish_pct:.0f}% | 📉 نزولی: {self.bearish_pct:.0f}%\n"
        msg += f"⏰ تایم‌فریم: {self.timeframe}\n"
        msg += f"🕐 زمان: {self.timestamp.strftime('%Y-%m-%d %H:%M')}\n\n"

        if self.direction in ["BUY", "SELL"]:
            msg += "📌 مشخصات معامله:\n" + "-"*30 + "\n"
            if self.entry_price: msg += f"🔹 ورود: {self.entry_price:.3f}\n"
            if self.stop_loss:
                msg += f"🛑 حد ضرر: {self.stop_loss:.3f} ({abs(self.entry_price-self.stop_loss)*100:.1f} پیپ)\n"
            if self.take_profit_1:
                msg += f"🎯 حد سود 1: {self.take_profit_1:.3f} ({abs(self.take_profit_1-self.entry_price)*100:.1f} پیپ)\n"
            if self.take_profit_2:
                msg += f"🎯 حد سود 2: {self.take_profit_2:.3f} ({abs(self.take_profit_2-self.entry_price)*100:.1f} پیپ)\n"
            if self.take_profit_3:
                msg += f"🎯 حد سود 3: {self.take_profit_3:.3f} ({abs(self.take_profit_3-self.entry_price)*100:.1f} پیپ)\n"
            if self.risk_reward > 0:
                msg += f"⚖️ ریسک/ریوارد: 1:{self.risk_reward:.1f}\n\n"

            # تحلیل پیشرفته
            msg += "🧠 تحلیل پیشرفته (SMC):\n" + "-"*30 + "\n"
            for method_name in ['smc', 'structure', 'supply_demand', 'volume_profile', 'harmonic', 'price_action']:
                if method_name in self.indicators:
                    res = self.indicators[method_name]
                    if isinstance(res, dict):
                        s = res.get("signal", "neutral")
                        d = res.get("description", "")
                        ic = "✅" if s == "bullish" else "❌" if s == "bearish" else "➖"
                        msg += f"{ic} {d}\n"

            # اندیکاتورهای کلاسیک
            msg += "\n📊 اندیکاتورهای کلاسیک:\n" + "-"*30 + "\n"
            for n in ['trend', 'rsi', 'macd', 'bollinger', 'stochastic', 'ichimoku']:
                if n in self.indicators:
                    i = self.indicators[n]
                    if isinstance(i, dict):
                        s = i.get("signal", "neutral")
                        d = i.get("description", "")
                        ic = "✅" if s == "bullish" else "❌" if s == "bearish" else "➖"
                        msg += f"{ic} {d}\n"

            # الگوهای کندلی
            if self.patterns:
                msg += "\n🕯 الگوهای کندلی:\n" + "-"*30 + "\n"
                for p in self.patterns:
                    ic = "🟢" if p["type"] == "bullish" else "🔴" if p["type"] == "bearish" else "⚪"
                    msg += f"{ic} {p['name_fa']}: {p['description']}\n"

            if self.warnings:
                msg += "\n⚠️ هشدارها:\n"
                for w in self.warnings: msg += f"⚠️ {w}\n"
        else:
            msg += "📋 وضعیت فعلی:\n" + "-"*30 + "\n"
            for n in ['smc', 'structure', 'supply_demand', 'volume_profile', 'harmonic', 'price_action', 'trend', 'rsi', 'macd']:
                if n in self.indicators:
                    i = self.indicators[n]
                    if isinstance(i, dict):
                        msg += f"➖ {i.get('description', '')}\n"

        msg += "\n" + "="*30 + "\n⚠️ این سیگنال صرفاً جنبه تحلیلی دارد."
        return msg


# ============================================================
# Advanced Analysis Engine (SMC, Market Structure, Supply/Demand,
# Volume Profile, Harmonic, Price Action)
# ============================================================
class SmartMoneyConcepts:
    """Smart Money Concepts - تشخیص حرکت پول هوشمند"""

    def find_order_blocks(self, df, lookback=50):
        """تشخیص Order Blocks"""
        recent = df.tail(lookback).copy()
        bullish_obs, bearish_obs = [], []
        try:
            for i in range(2, len(recent) - 5):
                if recent['close'].iloc[i] < recent['open'].iloc[i]:
                    move_up = recent['close'].iloc[i + 3] - recent['close'].iloc[i]
                    if move_up > recent['close'].iloc[i] * 0.001:
                        bullish_obs.append({
                            'price': (recent['high'].iloc[i] + recent['low'].iloc[i]) / 2,
                            'high': recent['high'].iloc[i], 'low': recent['low'].iloc[i],
                            'age': len(recent) - i,
                        })
                if recent['close'].iloc[i] > recent['open'].iloc[i]:
                    move_down = recent['close'].iloc[i] - recent['close'].iloc[i + 3]
                    if move_down > recent['close'].iloc[i] * 0.001:
                        bearish_obs.append({
                            'price': (recent['high'].iloc[i] + recent['low'].iloc[i]) / 2,
                            'high': recent['high'].iloc[i], 'low': recent['low'].iloc[i],
                            'age': len(recent) - i,
                        })
        except Exception as e:
            logger.error(f"خطا در OB: {e}")
        cp = df['close'].iloc[-1]
        valid_b = [ob for ob in bullish_obs if ob['age'] <= 30]
        valid_s = [ob for ob in bearish_obs if ob['age'] <= 30]
        nb = min(valid_b, key=lambda x: abs(x['price'] - cp)) if valid_b else None
        ns = min(valid_s, key=lambda x: abs(x['price'] - cp)) if valid_s else None
        return {'bullish_obs': valid_b, 'bearish_obs': valid_s, 'nearest_bullish_ob': nb, 'nearest_bearish_ob': ns}

    def find_fvg(self, df, lookback=50):
        """تشخیص Fair Value Gaps"""
        recent = df.tail(lookback).copy()
        bullish_fvgs, bearish_fvgs = [], []
        try:
            for i in range(2, len(recent)):
                if recent['low'].iloc[i] > recent['high'].iloc[i - 2]:
                    gap = recent['low'].iloc[i] - recent['high'].iloc[i - 2]
                    if gap > recent['close'].iloc[i] * 0.0005:
                        bullish_fvgs.append({'mid': (recent['low'].iloc[i] + recent['high'].iloc[i - 2]) / 2, 'age': len(recent) - i})
                if recent['high'].iloc[i] < recent['low'].iloc[i - 2]:
                    gap = recent['low'].iloc[i - 2] - recent['high'].iloc[i]
                    if gap > recent['close'].iloc[i] * 0.0005:
                        bearish_fvgs.append({'mid': (recent['low'].iloc[i - 2] + recent['high'].iloc[i]) / 2, 'age': len(recent) - i})
        except Exception as e:
            logger.error(f"خطا در FVG: {e}")
        return {'bullish_fvgs': [f for f in bullish_fvgs if f['age'] <= 20], 'bearish_fvgs': [f for f in bearish_fvgs if f['age'] <= 20]}

    def detect_bos_choch(self, df, lookback=50):
        """تشخیص BOS و CHoCH"""
        recent = df.tail(lookback).copy()
        cp = recent['close'].iloc[-1]
        swing_highs, swing_lows = [], []
        for i in range(2, len(recent) - 2):
            if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i-2]
                and recent['high'].iloc[i] > recent['high'].iloc[i+1] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                swing_highs.append({'price': recent['high'].iloc[i], 'index': i})
            if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i-2]
                and recent['low'].iloc[i] < recent['low'].iloc[i+1] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                swing_lows.append({'price': recent['low'].iloc[i], 'index': i})
        result = {'bos': None, 'choch': None, 'structure': 'neutral', 'description': 'ساختار نامشخص'}
        try:
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                last_sh, prev_sh = swing_highs[-1], swing_highs[-2]
                last_sl, prev_sl = swing_lows[-1], swing_lows[-2]
                if cp > last_sh['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(bos='bullish', structure='bullish', description='BOS صعودی - ادامه روند صعودی')
                elif cp < last_sl['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(bos='bearish', structure='bearish', description='BOS نزولی - ادامه روند نزولی')
                elif cp > last_sh['price'] and last_sl['index'] > prev_sl['index'] and last_sh['index'] < last_sl['index']:
                    result.update(choch='bullish', structure='bullish', description='CHoCH صعودی - تغییر روند به صعودی')
                elif cp < last_sl['price'] and last_sh['index'] > prev_sh['index'] and last_sl['index'] < last_sh['index']:
                    result.update(choch='bearish', structure='bearish', description='CHoCH نزولی - تغییر روند به نزولی')
        except Exception as e:
            logger.error(f"خطا در BOS/CHoCH: {e}")
        return result

    def premium_discount_zones(self, df, lookback=50):
        """مناطق Premium/Discount"""
        recent = df.tail(lookback)
        high = recent['high'].max()
        low = recent['low'].min()
        cp = df['close'].iloc[-1]
        mid = (high + low) / 2
        zone = 'premium' if cp > mid else 'discount'
        zone_fa = 'Premium (فروش)' if cp > mid else 'Discount (خرید)'
        pos = ((cp - low) / (high - low)) * 100 if (high - low) > 0 else 50
        return {'zone': zone, 'zone_fa': zone_fa, 'high': high, 'low': low, 'mid': mid, 'position_percent': pos}

    def analyze(self, df):
        try:
            obs = self.find_order_blocks(df)
            fvgs = self.find_fvg(df)
            structure = self.detect_bos_choch(df)
            zones = self.premium_discount_zones(df)
            bull = bear = 0
            if structure['structure'] == 'bullish': bull += 2
            elif structure['structure'] == 'bearish': bear += 2
            if zones['zone'] == 'discount': bull += 1
            elif zones['zone'] == 'premium': bear += 1
            cp = df['close'].iloc[-1]
            if obs['nearest_bullish_ob']:
                d = abs(obs['nearest_bullish_ob']['price'] - cp) / cp * 100
                if d < 0.5: bull += 2
                elif d < 1: bull += 1
            if obs['nearest_bearish_ob']:
                d = abs(obs['nearest_bearish_ob']['price'] - cp) / cp * 100
                if d < 0.5: bear += 2
                elif d < 1: bear += 1
            if fvgs['bullish_fvgs']:
                nf = min(fvgs['bullish_fvgs'], key=lambda x: x['age'])
                if nf['age'] <= 5: bull += 1
            if fvgs['bearish_fvgs']:
                nf = min(fvgs['bearish_fvgs'], key=lambda x: x['age'])
                if nf['age'] <= 5: bear += 1
            if bull > bear:
                return {'signal': 'bullish', 'score': min(100, (bull/7)*100), 'description': f"SMC: صعودی ({bull} امتیاز) - {structure['description']}", 'order_blocks': obs, 'fvgs': fvgs, 'structure': structure, 'zones': zones}
            elif bear > bull:
                return {'signal': 'bearish', 'score': min(100, (bear/7)*100), 'description': f"SMC: نزولی ({bear} امتیاز) - {structure['description']}", 'order_blocks': obs, 'fvgs': fvgs, 'structure': structure, 'zones': zones}
            return {'signal': 'neutral', 'score': 50, 'description': 'SMC: خنثی', 'order_blocks': obs, 'fvgs': fvgs, 'structure': structure, 'zones': zones}
        except Exception as e:
            logger.error(f"خطا در SMC: {e}")
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا در SMC: {e}'}


class MarketStructure:
    """تحلیل ساختار بازار - HH/HL/LH/LL"""
    def analyze(self, df, lookback=100):
        try:
            recent = df.tail(lookback)
            highs, lows = [], []
            for i in range(2, len(recent) - 2):
                if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i-2]
                    and recent['high'].iloc[i] > recent['high'].iloc[i+1] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                    highs.append({'price': recent['high'].iloc[i], 'index': i})
                if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i-2]
                    and recent['low'].iloc[i] < recent['low'].iloc[i+1] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                    lows.append({'price': recent['low'].iloc[i], 'index': i})
            if len(highs) < 2 or len(lows) < 2:
                return {'signal': 'neutral', 'score': 50, 'description': 'ساختار نامشخص'}
            hh = highs[-1]['price'] > highs[-2]['price']
            lh = highs[-1]['price'] < highs[-2]['price']
            hl = lows[-1]['price'] > lows[-2]['price']
            ll = lows[-1]['price'] < lows[-2]['price']
            if hh and hl: return {'signal': 'bullish', 'score': 85, 'description': 'ساختار: HH-HL (صعودی قوی)'}
            if ll and lh: return {'signal': 'bearish', 'score': 85, 'description': 'ساختار: LL-LH (نزولی قوی)'}
            if hh: return {'signal': 'bullish', 'score': 70, 'description': 'ساختار: Higher High'}
            if ll: return {'signal': 'bearish', 'score': 70, 'description': 'ساختار: Lower Low'}
            return {'signal': 'neutral', 'score': 50, 'description': 'ساختار: خنثی'}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}'}


class SupplyDemand:
    """تحلیل مناطق عرضه و تقاضا"""
    def find_zones(self, df, lookback=100):
        try:
            recent = df.tail(lookback).copy()
            demand_zones, supply_zones = [], []
            for i in range(4, len(recent) - 4):
                base = recent.iloc[i-2:i+1]
                rally = recent.iloc[i+1:i+4]
                if len(base) == 3 and len(rally) == 3:
                    if rally['close'].iloc[-1] > base['close'].iloc[-1] * 1.002:
                        demand_zones.append({'mid': (base['high'].max() + base['low'].min()) / 2, 'age': len(recent) - i})
                    if rally['close'].iloc[-1] < base['close'].iloc[-1] * 0.998:
                        supply_zones.append({'mid': (base['high'].max() + base['low'].min()) / 2, 'age': len(recent) - i})
            cp = df['close'].iloc[-1]
            vd = [z for z in demand_zones if z['age'] <= 30]
            vs = [z for z in supply_zones if z['age'] <= 30]
            nd = min(vd, key=lambda x: abs(x['mid'] - cp)) if vd else None
            ns = min(vs, key=lambda x: abs(x['mid'] - cp)) if vs else None
            return {'demand_zones': vd, 'supply_zones': vs, 'nearest_demand': nd, 'nearest_supply': ns}
        except Exception as e:
            logger.error(f"خطا در S/D: {e}")
            return {'demand_zones': [], 'supply_zones': []}

    def analyze(self, df):
        try:
            zones = self.find_zones(df)
            cp = df['close'].iloc[-1]
            bull = bear = 0
            if zones.get('nearest_demand'):
                d = abs(zones['nearest_demand']['mid'] - cp) / cp * 100
                if d < 0.3: bull += 2
                elif d < 0.6: bull += 1
            if zones.get('nearest_supply'):
                d = abs(zones['nearest_supply']['mid'] - cp) / cp * 100
                if d < 0.3: bear += 2
                elif d < 0.6: bear += 1
            if bull > bear: return {'signal': 'bullish', 'score': min(100, 60 + (bull - bear) * 10), 'description': 'عرضه/تقاضا: نزدیک Demand Zone (صعودی)', 'zones': zones}
            if bear > bull: return {'signal': 'bearish', 'score': min(100, 60 + (bear - bull) * 10), 'description': 'عرضه/تقاضا: نزدیک Supply Zone (نزولی)', 'zones': zones}
            return {'signal': 'neutral', 'score': 50, 'description': 'عرضه/تقاضا: خنثی', 'zones': zones}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}'}


class VolumeProfileAnalyzer:
    """تحلیل Volume Profile - POC, Value Area"""
    def analyze(self, df, bins=20, lookback=100):
        try:
            recent = df.tail(lookback).copy()
            if 'volume' not in recent.columns or recent['volume'].sum() == 0:
                return {'signal': 'neutral', 'score': 50, 'description': 'داده حجم در دسترس نیست'}
            pmin, pmax = recent['low'].min(), recent['high'].max()
            bsize = (pmax - pmin) / bins
            vp = []
            for i in range(bins):
                bl, bh = pmin + i * bsize, pmin + (i + 1) * bsize
                mask = (recent['low'] >= bl) & (recent['high'] <= bh)
                vp.append({'price': (bl + bh) / 2, 'volume': recent.loc[mask, 'volume'].sum()})
            poc = max(vp, key=lambda x: x['volume'])
            cp = df['close'].iloc[-1]
            if cp < poc['price']: return {'signal': 'bullish', 'score': 65, 'description': f'Volume: قیمت زیر POC (صعودی)', 'poc': poc['price']}
            if cp > poc['price']: return {'signal': 'bearish', 'score': 65, 'description': f'Volume: قیمت بالای POC (نزولی)', 'poc': poc['price']}
            return {'signal': 'neutral', 'score': 50, 'description': 'Volume: قیمت روی POC', 'poc': poc['price']}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}'}


class HarmonicPatterns:
    """تشخیص الگوهای هارمونیک"""
    def analyze(self, df):
        try:
            recent = df.tail(100)
            swings = []
            for i in range(2, len(recent) - 2):
                if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i+1]
                    and recent['high'].iloc[i] > recent['high'].iloc[i-2] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                    swings.append({'type': 'high', 'price': recent['high'].iloc[i]})
                if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i+1]
                    and recent['low'].iloc[i] < recent['low'].iloc[i-2] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                    swings.append({'type': 'low', 'price': recent['low'].iloc[i]})
            if len(swings) >= 4:
                pts = swings[-4:]
                x, a, b, c = pts[0], pts[1], pts[2], pts[3]
                xa = abs(a['price'] - x['price'])
                ab = abs(b['price'] - a['price'])
                if xa > 0:
                    ratio = ab / xa
                    if 0.5 <= ratio <= 0.7:
                        if x['type'] == 'high' and a['type'] == 'low' and b['type'] == 'high' and c['type'] == 'low':
                            return {'signal': 'bullish', 'score': 80, 'description': 'هارمونیک: Gartley صعودی'}
                        if x['type'] == 'low' and a['type'] == 'high' and b['type'] == 'low' and c['type'] == 'high':
                            return {'signal': 'bearish', 'score': 80, 'description': 'هارمونیک: Gartley نزولی'}
            return {'signal': 'neutral', 'score': 50, 'description': 'هارمونیک: الگویی نیست'}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}'}


class PriceActionAdvanced:
    """پرایس اکشن پیشرفته - الگوهای حرفه‌ای"""
    def detect_patterns(self, df):
        patterns = []
        if len(df) < 5: return patterns
        c, p = df.iloc[-1], df.iloc[-2]
        body_c = abs(c['close'] - c['open'])
        us_c = c['high'] - max(c['close'], c['open'])
        ls_c = min(c['close'], c['open']) - c['low']
        range_c = c['high'] - c['low']
        if range_c == 0: return patterns
        # Pin Bar Bullish
        if ls_c > 2 * body_c and ls_c > range_c * 0.6:
            patterns.append({'name_fa': 'پین بار صعودی', 'type': 'bullish', 'description': 'پین بار صعودی - بازگشت صعودی', 'strength': 70})
        # Pin Bar Bearish
        if us_c > 2 * body_c and us_c > range_c * 0.6:
            patterns.append({'name_fa': 'پین بار نزولی', 'type': 'bearish', 'description': 'پین بار نزولی - بازگشت نزولی', 'strength': 70})
        # Inside Bar
        if c['high'] < p['high'] and c['low'] > p['low']:
            if c['close'] > c['open']:
                patterns.append({'name_fa': 'اینساید بار صعودی', 'type': 'bullish', 'description': 'اینساید بار - شکست صعودی', 'strength': 60})
            else:
                patterns.append({'name_fa': 'اینساید بار نزولی', 'type': 'bearish', 'description': 'اینساید بار - شکست نزولی', 'strength': 60})
        # Engulfing
        if p['close'] < p['open'] and c['close'] > c['open'] and c['close'] > p['open'] and c['open'] < p['close']:
            patterns.append({'name_fa': 'پوشای صعودی', 'type': 'bullish', 'description': 'پوشای صعودی - خرید قوی', 'strength': 75})
        if p['close'] > p['open'] and c['close'] < c['open'] and c['open'] > p['close'] and c['close'] < p['open']:
            patterns.append({'name_fa': 'پوشای نزولی', 'type': 'bearish', 'description': 'پوشای نزولی - فروش قوی', 'strength': 75})
        # Hammer / Shooting Star
        if ls_c > body_c * 2 and us_c < body_c * 0.5 and body_c > 0:
            patterns.append({'name_fa': 'چکش', 'type': 'bullish', 'description': 'چکش - بازگشت از کف', 'strength': 70})
        if us_c > body_c * 2 and ls_c < body_c * 0.5 and body_c > 0:
            patterns.append({'name_fa': 'ستاره دنباله‌دار', 'type': 'bearish', 'description': 'ستاره دنباله‌دار - بازگشت از سقف', 'strength': 70})
        return patterns

    def analyze(self, df):
        try:
            patterns = self.detect_patterns(df)
            if not patterns:
                return {'signal': 'neutral', 'score': 50, 'description': 'پرایس اکشن: الگوی مشخصی نیست', 'patterns': []}
            bull = [p for p in patterns if p['type'] == 'bullish']
            bear = [p for p in patterns if p['type'] == 'bearish']
            if len(bull) > len(bear):
                return {'signal': 'bullish', 'score': np.mean([p['strength'] for p in bull]), 'description': f"پرایس اکشن: {len(bull)} الگوی صعودی", 'patterns': patterns}
            if len(bear) > len(bull):
                return {'signal': 'bearish', 'score': np.mean([p['strength'] for p in bear]), 'description': f"پرایس اکشن: {len(bear)} الگوی نزولی", 'patterns': patterns}
            return {'signal': 'neutral', 'score': 50, 'description': 'پرایس اکشن: متعادل', 'patterns': patterns}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}', 'patterns': []}


class AdvancedAnalysisEngine:
    """
    موتور تحلیل پیشرفته - ترکیب همه روش‌ها
    وزن: SMC(25%) + Structure(20%) + S/D(15%) + Volume(10%) + Harmonic(5%) + PriceAction(15%) + Classic(10%)
    """
    def __init__(self):
        self.smc = SmartMoneyConcepts()
        self.structure = MarketStructure()
        self.supply_demand = SupplyDemand()
        self.volume_profile = VolumeProfileAnalyzer()
        self.harmonic = HarmonicPatterns()
        self.price_action = PriceActionAdvanced()
        self.weights = {'smc': 0.25, 'structure': 0.20, 'supply_demand': 0.15, 'volume_profile': 0.10, 'harmonic': 0.05, 'price_action': 0.15, 'classic': 0.10}

    def analyze(self, df, classic_signals=None):
        try:
            results = {
                'smc': self.smc.analyze(df),
                'structure': self.structure.analyze(df),
                'supply_demand': self.supply_demand.analyze(df),
                'volume_profile': self.volume_profile.analyze(df),
                'harmonic': self.harmonic.analyze(df),
                'price_action': self.price_action.analyze(df),
            }
            if classic_signals:
                results['classic'] = {'signal': self._aggregate_classic(classic_signals), 'score': 60, 'description': 'اندیکاتورهای کلاسیک'}
            bull = bear = 0
            for method, res in results.items():
                if not isinstance(res, dict): continue
                sig = res.get('signal', 'neutral')
                w = self.weights.get(method, 0.1)
                sc = res.get('score', 50)
                if sig == 'bullish': bull += w * (sc / 100)
                elif sig == 'bearish': bear += w * (sc / 100)
            total = bull + bear
            bp = (bull / total * 100) if total > 0 else 50
            sp = (bear / total * 100) if total > 0 else 50
            diff = bp - sp
            if diff > 15:
                return {'direction': 'BUY', 'strength': min(100, 50 + abs(diff)), 'confidence': 'high' if diff > 30 else 'medium', 'bullish_pct': bp, 'bearish_pct': sp, 'results': results}
            if diff < -15:
                return {'direction': 'SELL', 'strength': min(100, 50 + abs(diff)), 'confidence': 'high' if diff < -30 else 'medium', 'bullish_pct': bp, 'bearish_pct': sp, 'results': results}
            return {'direction': 'WAIT', 'strength': 50, 'confidence': 'low', 'bullish_pct': bp, 'bearish_pct': sp, 'results': results}
        except Exception as e:
            logger.error(f"خطا در موتور پیشرفته: {e}", exc_info=True)
            return {'direction': 'WAIT', 'strength': 0, 'confidence': 'low', 'error': str(e)}

    def _aggregate_classic(self, classic_signals):
        bull = sum(1 for v in classic_signals.values() if isinstance(v, dict) and v.get('signal') == 'bullish')
        bear = sum(1 for v in classic_signals.values() if isinstance(v, dict) and v.get('signal') == 'bearish')
        return 'bullish' if bull > bear else 'bearish' if bear > bull else 'neutral'


# ============================================================
# Signal Engine (با موتور پیشرفته)
# ============================================================
class SignalEngine:
    def __init__(self):
        self.fetcher = DataFetcher()
        self.indicators = IndicatorsEngine()
        self.advanced = AdvancedAnalysisEngine()
        self._signal_history = self._load_history()

    def _load_history(self):
        try:
            if os.path.exists(SIGNALS_DB):
                with open(SIGNALS_DB, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except: pass
        return []

    def _save_history(self):
        try:
            if len(self._signal_history) > 1000:
                self._signal_history = self._signal_history[-1000:]
            with open(SIGNALS_DB, 'w', encoding='utf-8') as f:
                json.dump(self._signal_history, f, ensure_ascii=False, indent=2)
        except: pass

    def analyze(self, timeframe=PRIMARY_TIMEFRAME, multi_tf_confirm=True):
        signal = Signal()
        signal.timeframe = timeframe
        try:
            df = self.fetcher.fetch_ohlc(timeframe, count=CANDLE_COUNT)
            if df.empty or len(df) < 50:
                signal.description = "داده کافی نیست"
                return signal
            df = self.indicators.calculate_all(df)
            cp = float(df['close'].iloc[-1])
            signal.entry_price = cp

            # سیگنال‌های کلاسیک
            trend = self.indicators.get_trend_direction(df)
            rsi = self.indicators.get_rsi_signal(df)
            macd = self.indicators.get_macd_signal(df)
            bb = self.indicators.get_bollinger_signal(df)
            stoch = self.indicators.get_stochastic_signal(df)
            ichi = self.indicators.get_ichimoku_signal(df)
            vol = self.indicators.volume_analysis(df)
            classic_signals = {"trend": trend, "rsi": rsi, "macd": macd, "bollinger": bb, "stochastic": stoch, "ichimoku": ichi, "volume": vol}

            # تحلیل پیشرفته با موتور جدید
            advanced_result = self.advanced.analyze(df, classic_signals)

            # تنظیم سیگنال
            signal.direction = advanced_result['direction']
            signal.strength = advanced_result['strength']
            signal.confidence = advanced_result.get('confidence', 'low')
            signal.bullish_pct = advanced_result.get('bullish_pct', 50)
            signal.bearish_pct = advanced_result.get('bearish_pct', 50)

            # ذخیره اطلاعات برای نمایش
            signal.indicators = {
                "advanced": {"description": f"تحلیل پیشرفته: {signal.direction} (قدرت: {signal.strength:.0f}%)", "signal": "bullish" if signal.direction == "BUY" else "bearish" if signal.direction == "SELL" else "neutral"},
                "trend": trend, "rsi": rsi, "macd": macd, "bollinger": bb,
                "stochastic": stoch, "ichimoku": ichi, "volume": vol,
            }

            # اضافه کردن جزئیات روش‌های پیشرفته
            results = advanced_result.get('results', {})
            if 'smc' in results:
                signal.indicators["smc"] = results['smc']
            if 'structure' in results:
                signal.indicators["structure"] = results['structure']
            if 'supply_demand' in results:
                signal.indicators["supply_demand"] = results['supply_demand']
            if 'price_action' in results:
                pa = results['price_action']
                signal.indicators["price_action"] = pa
                signal.patterns = pa.get('patterns', [])

            # محاسبه امتیاز
            total_methods = len(results)
            bull_count = sum(1 for v in results.values() if isinstance(v, dict) and v.get('signal') == 'bullish')
            bear_count = sum(1 for v in results.values() if isinstance(v, dict) and v.get('signal') == 'bearish')
            signal.score = max(bull_count, bear_count)
            signal.max_score = total_methods

            # تأیید چند تایم‌فریمی
            if multi_tf_confirm and signal.direction != "WAIT":
                if not self._multi_timeframe_confirmation(signal.direction):
                    signal.warnings.append("تأیید چند تایم‌فریمی ضعیف")
                    if signal.strength > 70: signal.strength = 70

            # محاسبه SL/TP
            if signal.direction != "WAIT":
                self._calculate_sl_tp(df, signal)

            self._signal_history.append(signal.to_dict())
            self._save_history()

            logger.info(f"سیگنال پیشرفته: {signal.direction} (قدرت: {signal.strength:.0f}%, صعودی: {signal.bullish_pct:.0f}%, نزولی: {signal.bearish_pct:.0f}%)")
            return signal

        except Exception as e:
            logger.error(f"خطا در تحلیل: {e}", exc_info=True)
            signal.description = f"خطا: {str(e)}"
            return signal

    def _multi_timeframe_confirmation(self, direction):
        try:
            conf = tot = 0
            for tf in [SECONDARY_TIMEFRAME, TREND_TIMEFRAME]:
                try:
                    df = self.fetcher.fetch_ohlc(tf, count=200)
                    if df.empty: continue
                    df = self.indicators.calculate_all(df)
                    t = self.indicators.get_trend_direction(df)
                    tot += 1
                    if direction == "BUY" and t["direction"] == "bullish": conf += 1
                    elif direction == "SELL" and t["direction"] == "bearish": conf += 1
                except: pass
            return conf >= tot / 2 if tot > 0 else True
        except: return True

    def _calculate_sl_tp(self, df, signal):
        try:
            atr_v = df['atr'].iloc[-1]
            if pd.isna(atr_v) or atr_v == 0:
                atr_v = df['close'].iloc[-1] * 0.002
            e = signal.entry_price
            if signal.direction == "BUY":
                signal.stop_loss = e - (atr_v * ATR_SL_MULTIPLIER)
                r = e - signal.stop_loss
                signal.take_profit_1 = e + (r * 1.0)
                signal.take_profit_2 = e + (r * 2.0)
                signal.take_profit_3 = e + (r * 3.0)
                signal.risk_reward = 3.0
            elif signal.direction == "SELL":
                signal.stop_loss = e + (atr_v * ATR_SL_MULTIPLIER)
                r = signal.stop_loss - e
                signal.take_profit_1 = e - (r * 1.0)
                signal.take_profit_2 = e - (r * 2.0)
                signal.take_profit_3 = e - (r * 3.0)
                signal.risk_reward = 3.0
        except Exception as e:
            logger.error(f"خطا در SL/TP: {e}")

    def get_status_summary(self):
        try:
            df = self.fetcher.fetch_ohlc(PRIMARY_TIMEFRAME, count=CANDLE_COUNT)
            if df.empty: return {"error": "داده در دسترس نیست"}
            df = self.indicators.calculate_all(df)
            return {
                "symbol": SYMBOL_DISPLAY, "price": float(df['close'].iloc[-1]),
                "timestamp": datetime.now().isoformat(),
                "trend": self.indicators.get_trend_direction(df),
                "rsi": self.indicators.get_rsi_signal(df),
                "macd": self.indicators.get_macd_signal(df),
                "bollinger": self.indicators.get_bollinger_signal(df),
                "stochastic": self.indicators.get_stochastic_signal(df),
                "ichimoku": self.indicators.get_ichimoku_signal(df),
                "volume": self.indicators.volume_analysis(df),
                "support_resistance": self.indicators.detect_support_resistance(df),
                "fibonacci": self.indicators.fibonacci_retracement(df),
                "timeframe": PRIMARY_TIMEFRAME,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_signal_history(self, limit=20):
        return self._signal_history[-limit:] if self._signal_history else []

    def get_performance_stats(self):
        if not self._signal_history:
            return {"total_signals": 0, "buy_signals": 0, "sell_signals": 0, "wait_signals": 0, "strong_signals": 0}
        t = len(self._signal_history)
        b = sum(1 for s in self._signal_history if s["direction"] == "BUY")
        s = sum(1 for s in self._signal_history if s["direction"] == "SELL")
        w = sum(1 for s in self._signal_history if s["direction"] == "WAIT")
        st = sum(1 for s in self._signal_history if s.get("confirmed", False))
        return {"total_signals": t, "buy_signals": b, "sell_signals": s, "wait_signals": w, "strong_signals": st}


# ============================================================
# Economic Calendar
# ============================================================
class EconomicCalendar:
    def __init__(self):
        self._cache = None
        self._cache_time = None
        self._cache_ttl = 3600

    def fetch_events(self, force=False):
        if not force and self._cache and self._cache_time:
            if time.time() - self._cache_time < self._cache_ttl:
                return self._cache
        try:
            r = requests.get(ECON_CALENDAR_URL, timeout=15)
            r.raise_for_status()
            events = r.json()
            filtered = []
            for e in events:
                if e.get("country", "") in ["USD", "JPY", "All"]:
                    if not HIGH_IMPACT_ONLY or e.get("impact", "") in ["High", "high", "HIGH"]:
                        filtered.append(e)
            self._cache = filtered
            self._cache_time = time.time()
            return filtered
        except Exception as e:
            logger.error(f"خطا در تقویم اقتصادی: {e}")
            return self._cache if self._cache else []

    def is_news_blackout(self, minutes=NEWS_BLACKOUT_MINUTES):
        events = self.fetch_events()
        now = datetime.utcnow()
        bs = now - timedelta(minutes=minutes)
        be = now + timedelta(minutes=minutes)
        for e in events:
            try:
                ds = e.get("date", "")
                if not ds: continue
                ed = datetime.fromisoformat(ds.replace("Z", "+00:00"))
                if bs <= ed <= be:
                    return {"is_blackout": True, "event": e, "event_time": ed, "title": e.get("title", ""), "country": e.get("country", "")}
            except: continue
        return {"is_blackout": False}

    def format_events_persian(self, events, limit=10):
        if not events:
            return "📅 تقویم اقتصادی\n\nرویداد مهمی وجود ندارد."
        flags = {"USD": "🇺🇸", "JPY": "🇯🇵"}
        msg = "📅 تقویم اقتصادی هفته\n" + "="*30 + "\n\n"
        for i, e in enumerate(events[:limit]):
            c = e.get("country", "")
            f = flags.get(c, "🌐")
            t = e.get("title", "نامشخص")
            try:
                ed = datetime.fromisoformat(e.get("date", "").replace("Z", "+00:00"))
                ts = ed.strftime("%m/%d %H:%M UTC")
            except: ts = e.get("date", "")
            msg += f"{i+1}. {f} {t}\n   ⏰ {ts}\n   🔴 مهم\n"
            if e.get("forecast", "N/A") != "N/A":
                msg += f"   📊 پیش‌بینی: {e['forecast']} | قبلی: {e.get('previous', 'N/A')}\n"
            msg += "\n"
        return msg


# ============================================================
# Risk Manager
# ============================================================
@dataclass
class Trade:
    id: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_1: Optional[float]
    take_profit_2: Optional[float]
    take_profit_3: Optional[float]
    position_size: float
    risk_amount: float
    risk_pips: float
    open_time: str
    status: str = "open"
    close_price: Optional[float] = None
    close_time: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pips: Optional[float] = None


class RiskManager:
    def __init__(self):
        self.account_balance = ACCOUNT_BALANCE
        self.starting_balance = ACCOUNT_BALANCE
        self.trades = self._load_trades()
        self.daily_pnl = 0
        self.daily_reset_date = date.today()

    def _load_trades(self):
        try:
            if os.path.exists(TRADES_DB):
                with open(TRADES_DB, 'r', encoding='utf-8') as f:
                    return [Trade(**t) for t in json.load(f)]
        except: pass
        return []

    def _save_trades(self):
        try:
            with open(TRADES_DB, 'w', encoding='utf-8') as f:
                json.dump([asdict(t) for t in self.trades], f, ensure_ascii=False, indent=2)
        except: pass

    def calculate_position_size(self, entry, sl, direction="BUY"):
        pip_val = 1000
        sl_dist = abs(entry - sl)
        sl_pips = sl_dist * 100
        if sl_pips == 0: return {"error": "فاصله SL صفر"}
        risk_amt = self.account_balance * RISK_PER_TRADE
        lots = risk_amt / (sl_pips * pip_val)
        max_lots = self.account_balance / 10000
        lots = min(lots, max_lots)
        lots = max(lots, 0.01)
        return {"position_size_lots": round(lots, 2), "risk_amount": round(risk_amt, 2), "sl_pips": round(sl_pips, 1), "position_value": round(lots * 100000 * entry, 2)}

    def can_open_trade(self, direction=None):
        if self.daily_reset_date != date.today():
            self.daily_pnl = 0
            self.daily_reset_date = date.today()
        dm = self.starting_balance * DAILY_MAX_LOSS
        if self.daily_pnl <= -dm:
            return {"can_open": False, "reason": "حد ضرر روزانه رسید"}
        ot = [t for t in self.trades if t.status == "open"]
        if len(ot) >= MAX_CONCURRENT_TRADES:
            return {"can_open": False, "reason": "حداکثر معاملات همزمان"}
        return {"can_open": True, "reason": "اجازه باز کردن"}

    def get_account_summary(self):
        ot = [t for t in self.trades if t.status == "open"]
        ct = [t for t in self.trades if t.status != "open"]
        tp = sum(t.pnl for t in ct if t.pnl)
        wt = [t for t in ct if t.pnl and t.pnl > 0]
        lt = [t for t in ct if t.pnl and t.pnl < 0]
        wr = (len(wt) / len(ct) * 100) if ct else 0
        return {"current_balance": self.account_balance + tp, "starting_balance": self.starting_balance, "total_pnl": tp, "daily_pnl": self.daily_pnl, "open_trades": len(ot), "total_trades": len(ct), "winning_trades": len(wt), "losing_trades": len(lt), "win_rate": wr, "max_drawdown": 0}


# ============================================================
# Globals
# ============================================================
_data_fetcher = None
_signal_engine = None
_calendar = None
_risk_manager = None

def get_data_fetcher():
    global _data_fetcher
    if _data_fetcher is None: _data_fetcher = DataFetcher()
    return _data_fetcher

def get_signal_engine():
    global _signal_engine
    if _signal_engine is None: _signal_engine = SignalEngine()
    return _signal_engine

def get_calendar():
    global _calendar
    if _calendar is None: _calendar = EconomicCalendar()
    return _calendar

def get_risk_manager():
    global _risk_manager
    if _risk_manager is None: _risk_manager = RiskManager()
    return _risk_manager


def is_authorized(user_id):
    if not TELEGRAM_ADMIN_IDS: return True
    return user_id in TELEGRAM_ADMIN_IDS


def format_full_analysis(status):
    msg = f"📊 تحلیل کامل USD/JPY\n{'='*40}\n\n"
    if "error" in status:
        return msg + f"❌ خطا: {status['error']}"
    msg += f"💵 قیمت: {status.get('price', 'N/A')}\n⏰ زمان: {datetime.now().strftime('%H:%M:%S')}\n\n"
    for k in ["trend", "rsi", "macd", "bollinger", "stochastic", "ichimoku", "volume"]:
        d = status.get(k, {})
        if d and isinstance(d, dict):
            msg += f"• {d.get('description', 'N/A')}\n"
    sr = status.get("support_resistance", {})
    if sr:
        msg += f"\n🔴 مقاومت: {sr.get('resistances', [])}\n🟢 حمایت: {sr.get('supports', [])}\n"
    return msg


# ============================================================
# Telegram Handlers
# ============================================================
async def start_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text(
        "👋 به ربات سیگنال USD/JPY خوش آمدید!\n\n"
        "دستورات:\n/signal - سیگنال فعلی\n/analysis - تحلیل کامل\n/price - قیمت لحظه‌ای\n"
        "/status - وضعیت معاملات\n/news - اخبار اقتصادی\n/ai - تحلیل AI\n"
        "/stats - آمار عملکرد\n/help - راهنما\n\n"
        "⚠️ این سیگنال‌ها صرفاً جنبه تحلیلی دارند."
    )

async def help_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text(
        "📋 راهنما\n\n"
        "/signal - دریافت سیگنال\n/analysis - تحلیل کامل\n/price - قیمت لحظه‌ای\n"
        "/status - وضعیت معاملات\n/news - اخبار هفته\n/ai - تحلیل AI\n"
        "/stats - آمار عملکرد\n/start - شروع\n/about - درباره\n/help - این راهنما"
    )

async def signal_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    msg = await update.message.reply_text("⏳ در حال تحلیل بازار...")
    try:
        loop = asyncio.get_event_loop()
        signal = await loop.run_in_executor(None, lambda: get_signal_engine().analyze(PRIMARY_TIMEFRAME))
        if CHECK_NEWS_BEFORE_SIGNAL:
            cal = get_calendar()
            bo = await loop.run_in_executor(None, cal.is_news_blackout)
            if bo.get("is_blackout"):
                signal.warnings.append(f"⚠️ خبر مهم: {bo.get('title')}")
        await msg.delete()
        await update.message.reply_text(signal.to_persian_message())
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {e}")

async def analysis_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    msg = await update.message.reply_text("⏳ تحلیل کامل...")
    try:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, lambda: get_signal_engine().get_status_summary())
        await msg.delete()
        await update.message.reply_text(format_full_analysis(status))
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {e}")

async def price_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: get_data_fetcher().get_market_info())
        if not info:
            await update.message.reply_text("❌ خطا در دریافت قیمت")
            return
        ce = "📈" if info.get('change', 0) >= 0 else "📉"
        cs = "+" if info.get('change', 0) >= 0 else ""
        await update.message.reply_text(
            f"💰 قیمت USD/JPY\n{'='*30}\n\n"
            f"💵 قیمت: {info['current_price']:.3f}\n"
            f"{ce} تغییر: {cs}{info['change']:.3f} ({cs}{info['change_pct']:.2f}%)\n"
            f"⬆️ سقف 24h: {info['high_24h']:.3f}\n"
            f"⬇️ کف 24h: {info['low_24h']:.3f}\n"
            f"🕐 {info['last_update']}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def status_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    try:
        rm = get_risk_manager()
        s = rm.get_account_summary()
        ot = [t for t in rm.trades if t.status == "open"]
        msg = "📊 وضعیت حساب و معاملات\n" + "="*35 + "\n\n"
        msg += "💰 حساب:\n"
        msg += f"• موجودی: ${s['current_balance']:,.2f}\n"
        msg += f"• سود/ضرر: ${s['total_pnl']:,.2f}\n"
        msg += f"• معاملات باز: {s['open_trades']}/{MAX_CONCURRENT_TRADES}\n\n"
        if ot:
            msg += f"📋 معاملات باز ({len(ot)}):\n"
            for i, t in enumerate(ot):
                ic = "🟢" if t.direction == "BUY" else "🔴"
                msg += f"\n{i+1}. {ic} {t.direction}\n   ورود: {t.entry_price:.3f}\n   SL: {t.stop_loss:.3f}\n"
        else:
            msg += "📭 معامله بازی نیست\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def news_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    msg = await update.message.reply_text("⏳ دریافت اخبار...")
    try:
        loop = asyncio.get_event_loop()
        cal = get_calendar()
        events = await loop.run_in_executor(None, cal.fetch_events)
        if not events:
            await msg.edit_text("📅 تقویم اقتصادی در دسترس نیست.")
            return
        await msg.delete()
        await update.message.reply_text(cal.format_events_persian(events))
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {e}")

async def ai_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    if not AI_ANALYSIS_ENABLED:
        await update.message.reply_text("⚠️ تحلیل AI فعال نیست.\nبرای فعال‌سازی، ZAI_API_KEY را در فایل .env تنظیم کنید.")
        return
    msg = await update.message.reply_text("🤖 تحلیل AI...")
    try:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, lambda: get_signal_engine().get_status_summary())
        signal = await loop.run_in_executor(None, lambda: get_signal_engine().analyze(PRIMARY_TIMEFRAME))
        # فراخوانی AI
        prompt = f"بازار USD/JPY را تحلیل کن. قیمت: {status.get('price')}. روند: {status.get('trend', {}).get('description')}. سیگنال: {signal.direction}. لطفاً تحلیل کوتاه فارسی بده."
        try:
            r = requests.post(
                "https://api.z.ai/api/paas/v4/chat/completions",
                headers={"Authorization": f"Bearer {ZAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 800},
                timeout=60
            )
            r.raise_for_status()
            ai_resp = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as ae:
            ai_resp = f"خطا در تحلیل AI: {ae}"
        await msg.delete()
        await update.message.reply_text(f"🤖 تحلیل هوش مصنوعی\n{'='*30}\n\n{ai_resp}")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {e}")

async def stats_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    try:
        se = get_signal_engine()
        st = se.get_performance_stats()
        rm = get_risk_manager()
        ac = rm.get_account_summary()
        msg = "📊 آمار عملکرد\n" + "="*35 + "\n\n"
        msg += "📈 سیگنال‌ها:\n"
        msg += f"• کل: {st['total_signals']}\n• خرید: {st['buy_signals']}\n• فروش: {st['sell_signals']}\n• صبر: {st['wait_signals']}\n• قوی: {st['strong_signals']}\n\n"
        msg += "💰 حساب:\n"
        msg += f"• موجودی: ${ac['current_balance']:,.2f}\n• نرخ برد: {ac['win_rate']:.1f}%\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def about_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text(
        "ℹ️ درباره ربات\n\n"
        "🤖 ربات سیگنال‌دهی USD/JPY\n"
        "📊 با تحلیل تکنیکال پیشرفته\n"
        "🎯 اندیکاتورها: RSI, MACD, EMA, Bollinger, Stochastic, Ichimoku, ATR\n"
        "💼 مدیریت ریسک خودکار\n"
        "📅 تقویم اقتصادی\n"
        "🤖 تحلیل هوش مصنوعی\n\n"
        "⚠️ صرفاً جنبه آموزشی دارد.\nنسخه: 1.0.0"
    )


# ============================================================
# Scheduler
# ============================================================
async def scheduled_signal_check(application):
    try:
        logger.info("🔍 چک خودکار سیگنال...")
        loop = asyncio.get_event_loop()
        if CHECK_NEWS_BEFORE_SIGNAL:
            cal = get_calendar()
            bo = await loop.run_in_executor(None, cal.is_news_blackout)
            if bo.get("is_blackout"):
                logger.info(f"🚫 خاموشی خبری: {bo.get('title')}")
                return
        signal = await loop.run_in_executor(None, lambda: get_signal_engine().analyze(PRIMARY_TIMEFRAME))
        history = get_signal_engine().get_signal_history(limit=2)
        is_new = False
        if len(history) >= 2:
            prev = history[-2]
            if prev["direction"] != signal.direction and signal.direction != "WAIT":
                is_new = True
        elif signal.direction != "WAIT":
            is_new = True
        if is_new and signal.strength >= 60:
            logger.info(f"📢 سیگنال جدید: {signal.direction}")
            msg = signal.to_persian_message()
            if TELEGRAM_CHAT_ID:
                try:
                    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
                except Exception as e:
                    logger.error(f"خطا در ارسال: {e}")
        else:
            logger.info(f"ℹ️ سیگنال جدیدی نیست: {signal.direction} ({signal.strength:.0f}%)")
    except Exception as e:
        logger.error(f"خطا در چک خودکار: {e}", exc_info=True)


# ============================================================
# Main
# ============================================================
# ============================================================
# Global Scheduler (شروع در post_init)
# ============================================================
scheduler = None


async def post_init(application):
    """پس از راه‌اندازی ربات - شروع زمان‌بند"""
    global scheduler
    
    logger.info("=" * 60)
    logger.info("🤖 ربات سیگنال USD/JPY راه‌اندازی شد")
    logger.info("=" * 60)
    bot_info = await application.bot.get_me()
    logger.info(f"📋 ربات: @{bot_info.username}")
    logger.info(f"📊 نماد: {SYMBOL_DISPLAY}")
    logger.info(f"⏱ تایم‌فریم: {PRIMARY_TIMEFRAME}")
    
    # شروع زمان‌بند داخل event loop
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_signal_check,
        args=[application],
        trigger=IntervalTrigger(minutes=SIGNAL_CHECK_INTERVAL),
        id="signal_check"
    )
    scheduler.start()
    logger.info("✓ زمان‌بند فعال شد")
    
    if TELEGRAM_CHAT_ID:
        try:
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"✅ ربات سیگنال USD/JPY فعال شد!\n\n📊 نماد: {SYMBOL_DISPLAY}\n⏱ تایم‌فریم: {PRIMARY_TIMEFRAME}\n🔄 چک هر: {SIGNAL_CHECK_INTERVAL} دقیقه\n\n/signal را بزنید."
            )
        except Exception as e:
            logger.error(f"خطا در ارسال شروع: {e}")


def main():
    print("\n" + "=" * 60)
    print("🤖 ربات سیگنال‌دهی USD/JPY")
    print("=" * 60)

    if not TELEGRAM_BOT_TOKEN:
        print("\n❌ توکن ربات تنظیم نشده!")
        print("لطفاً فایل .env را در همین فولدر بسازید:")
        print("""
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
TELEGRAM_ADMIN_IDS=your_chat_id_here
""")
        print("سپس دوباره python bot.py را اجرا کنید.")
        sys.exit(1)

    if not TELEGRAM_CHAT_ID:
        print("\n❌ TELEGRAM_CHAT_ID تنظیم نشده!")
        sys.exit(1)

    print(f"\n📋 توکن: {TELEGRAM_BOT_TOKEN[:15]}...")
    print(f"👤 چت ID: {TELEGRAM_CHAT_ID}")
    print(f"📊 نماد: {SYMBOL_DISPLAY}")
    print(f"⏱ تایم‌فریم: {PRIMARY_TIMEFRAME}")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("signal", signal_cmd))
    application.add_handler(CommandHandler("analysis", analysis_cmd))
    application.add_handler(CommandHandler("price", price_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("news", news_cmd))
    application.add_handler(CommandHandler("ai", ai_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("about", about_cmd))

    print("\n🚀 راه‌اندازی ربات...")
    print("برای توقف: Ctrl+C\n")

    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except KeyboardInterrupt:
        print("\n🛑 توقف ربات")
    except Exception as e:
        logger.error(f"❌ خطا: {e}", exc_info=True)
    finally:
        if scheduler:
            scheduler.shutdown()


if __name__ == "__main__":
    from datetime import timedelta
    main()

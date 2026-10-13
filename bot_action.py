# -*- coding: utf-8 -*-
"""
ربات سیگنال‌دهی USD/JPY - نسخه GitHub Actions
USD/JPY Signal Bot - GitHub Actions Version

این نسخه برای اجرا در GitHub Actions طراحی شده:
- هر 30 دقیقه اجرا می‌شه
- سیگنال رو چک می‌کنه
- اگه سیگنال جدید بود، به تلگرام می‌فرسته

نحوه اجرا:
python bot_action.py
"""

import os
import sys
import json
import logging
from datetime import datetime

# ============================================================
# نصب خودکار وابستگی‌ها
# ============================================================
def install_dependencies():
    """نصب خودکار وابستگی‌ها"""
    deps = ["yfinance", "pandas", "numpy", "requests", "python-dotenv"]
    for dep in deps:
        os.system(f'pip install {dep} --quiet')
    print("✅ وابستگی‌ها نصب شدند")

install_dependencies()

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
    from dotenv import load_dotenv
except ImportError as e:
    print(f"❌ خطا در ایمپورت: {e}")
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
CANDLE_COUNT = 300

# Indicator parameters
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
EMA_FAST = 21
EMA_MID = 50
EMA_SLOW = 200
BB_PERIOD = 20
BB_STD = 2.0
STOCH_PERIOD = 14
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5

# تنظیمات ذخیره سیگنال (در GitHub Actions از artifacts استفاده می‌کنیم)
SIGNAL_STATE_FILE = "signal_state.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# Data Fetcher
# ============================================================
class DataFetcher:
    def __init__(self):
        self.ticker = yf.Ticker(SYMBOL)

    def fetch_ohlc(self, timeframe=PRIMARY_TIMEFRAME, count=CANDLE_COUNT):
        try:
            if timeframe == "15m":
                period = "60d"
            elif timeframe in ("1h", "60m"):
                period = "730d"
            elif timeframe == "4h":
                period = "60d"
            elif timeframe == "1d":
                period = "2y"
            else:
                period = "60d"

            df = self.ticker.history(period=period, interval=timeframe, auto_adjust=False, prepost=False)
            if df.empty:
                df = yf.download(SYMBOL, period=period, interval=timeframe, progress=False, auto_adjust=False)
            if df.empty:
                raise ValueError("داده خالی")

            df.columns = [str(c).lower().replace(' ', '_') for c in df.columns]
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    if col == 'volume':
                        df[col] = 0
            df = df.dropna(subset=['open', 'high', 'low', 'close'])
            if len(df) > count:
                df = df.tail(count)
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده: {e}")
            raise


# ============================================================
# Indicators Engine (نسخه فشرده)
# ============================================================
class IndicatorsEngine:
    def ema(self, s, p): return s.ewm(span=p, adjust=False).mean()
    def sma(self, s, p): return s.rolling(window=p).mean()

    def calculate_all(self, df):
        df = df.copy()
        df['ema_21'] = self.ema(df['close'], EMA_FAST)
        df['ema_50'] = self.ema(df['close'], EMA_MID)
        df['ema_200'] = self.ema(df['close'], EMA_SLOW)
        df['rsi'] = self.rsi(df['close'])
        m, s, h = self.macd(df['close'])
        df['macd'], df['macd_signal'], df['macd_histogram'] = m, s, h
        bb_m, bb_u, bb_l, _ = self.bollinger_bands(df['close'])
        df['bb_middle'], df['bb_upper'], df['bb_lower'] = bb_m, bb_u, bb_l
        k, d = self.stochastic(df)
        df['stoch_k'], df['stoch_d'] = k, d
        df['atr'] = self.atr(df)
        if 'volume' in df.columns and df['volume'].sum() > 0:
            df['volume_ma'] = self.sma(df['volume'], 20)
        return df

    def rsi(self, s, p=RSI_PERIOD):
        delta = s.diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/p, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/p, adjust=False).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def macd(self, s):
        ema_fast = self.ema(s, MACD_FAST)
        ema_slow = self.ema(s, MACD_SLOW)
        macd_line = ema_fast - ema_slow
        signal_line = self.ema(macd_line, MACD_SIGNAL)
        return macd_line, signal_line, macd_line - signal_line

    def bollinger_bands(self, s):
        middle = self.sma(s, BB_PERIOD)
        std = s.rolling(window=BB_PERIOD).std()
        return middle, middle + (std * BB_STD), middle - (std * BB_STD), (middle - middle) / middle

    def stochastic(self, df):
        low_min = df['low'].rolling(window=STOCH_PERIOD).min()
        high_max = df['high'].rolling(window=STOCH_PERIOD).max()
        k_fast = 100 * ((df['close'] - low_min) / (high_max - low_min))
        return k_fast.rolling(window=3).mean(), k_fast.rolling(window=3).rolling(window=3).mean()

    def atr(self, df):
        hl = df['high'] - df['low']
        hc = (df['high'] - df['close'].shift()).abs()
        lc = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()


# ============================================================
# Smart Money Concepts (نسخه فشرده)
# ============================================================
class SmartMoneyConcepts:
    def find_order_blocks(self, df, lookback=50):
        recent = df.tail(lookback).copy()
        bullish_obs, bearish_obs = [], []
        try:
            for i in range(2, min(len(recent) - 5, lookback - 5)):
                if recent['close'].iloc[i] < recent['open'].iloc[i]:
                    move_up = recent['close'].iloc[i + 3] - recent['close'].iloc[i]
                    if move_up > recent['close'].iloc[i] * 0.001:
                        bullish_obs.append({'price': (recent['high'].iloc[i] + recent['low'].iloc[i]) / 2, 'age': len(recent) - i})
                if recent['close'].iloc[i] > recent['open'].iloc[i]:
                    move_down = recent['close'].iloc[i] - recent['close'].iloc[i + 3]
                    if move_down > recent['close'].iloc[i] * 0.001:
                        bearish_obs.append({'price': (recent['high'].iloc[i] + recent['low'].iloc[i]) / 2, 'age': len(recent) - i})
        except Exception as e:
            logger.error(f"خطا در OB: {e}")
        cp = df['close'].iloc[-1]
        valid_b = [ob for ob in bullish_obs if ob['age'] <= 30]
        valid_s = [ob for ob in bearish_obs if ob['age'] <= 30]
        nb = min(valid_b, key=lambda x: abs(x['price'] - cp)) if valid_b else None
        ns = min(valid_s, key=lambda x: abs(x['price'] - cp)) if valid_s else None
        return {'nearest_bullish_ob': nb, 'nearest_bearish_ob': ns}

    def detect_bos_choch(self, df, lookback=50):
        recent = df.tail(lookback).copy()
        cp = recent['close'].iloc[-1]
        swing_highs, swing_lows = [], []
        for i in range(2, min(len(recent) - 2, lookback - 2)):
            try:
                if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i-2]
                    and recent['high'].iloc[i] > recent['high'].iloc[i+1] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                    swing_highs.append({'price': recent['high'].iloc[i], 'index': i})
                if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i-2]
                    and recent['low'].iloc[i] < recent['low'].iloc[i+1] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                    swing_lows.append({'price': recent['low'].iloc[i], 'index': i})
            except:
                continue
        result = {'bos': None, 'choch': None, 'structure': 'neutral', 'description': 'ساختار نامشخص'}
        try:
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                last_sh, prev_sh = swing_highs[-1], swing_highs[-2]
                last_sl, prev_sl = swing_lows[-1], swing_lows[-2]
                if cp > last_sh['price'] and last_sh['index'] > prev_sh['index']:
                    result.update(structure='bullish', description='BOS صعودی')
                elif cp < last_sl['price'] and last_sl['index'] > prev_sl['index']:
                    result.update(structure='bearish', description='BOS نزولی')
                elif cp > last_sh['price'] and last_sl['index'] > prev_sl['index'] and last_sh['index'] < last_sl['index']:
                    result.update(structure='bullish', description='CHoCH صعودی')
                elif cp < last_sl['price'] and last_sh['index'] > prev_sh['index'] and last_sl['index'] < last_sh['index']:
                    result.update(structure='bearish', description='CHoCH نزولی')
        except Exception as e:
            logger.error(f"خطا در BOS/CHoCH: {e}")
        return result

    def premium_discount_zones(self, df, lookback=50):
        recent = df.tail(lookback)
        high = recent['high'].max()
        low = recent['low'].min()
        cp = df['close'].iloc[-1]
        mid = (high + low) / 2
        return {'zone': 'discount' if cp <= mid else 'premium', 'mid': mid}

    def analyze(self, df):
        try:
            obs = self.find_order_blocks(df)
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
            if bull > bear:
                return {'signal': 'bullish', 'score': min(100, (bull/7)*100), 'description': f"SMC: صعودی ({bull} امتیاز) - {structure['description']}"}
            elif bear > bull:
                return {'signal': 'bearish', 'score': min(100, (bear/7)*100), 'description': f"SMC: نزولی ({bear} امتیاز) - {structure['description']}"}
            return {'signal': 'neutral', 'score': 50, 'description': 'SMC: خنثی'}
        except Exception as e:
            logger.error(f"خطا در SMC: {e}")
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}'}


# ============================================================
# Market Structure
# ============================================================
class MarketStructure:
    def analyze(self, df, lookback=100):
        try:
            recent = df.tail(lookback)
            highs, lows = [], []
            for i in range(2, min(len(recent) - 2, lookback - 2)):
                if (recent['high'].iloc[i] > recent['high'].iloc[i-1] and recent['high'].iloc[i] > recent['high'].iloc[i-2]
                    and recent['high'].iloc[i] > recent['high'].iloc[i+1] and recent['high'].iloc[i] > recent['high'].iloc[i+2]):
                    highs.append(recent['high'].iloc[i])
                if (recent['low'].iloc[i] < recent['low'].iloc[i-1] and recent['low'].iloc[i] < recent['low'].iloc[i-2]
                    and recent['low'].iloc[i] < recent['low'].iloc[i+1] and recent['low'].iloc[i] < recent['low'].iloc[i+2]):
                    lows.append(recent['low'].iloc[i])
            if len(highs) < 2 or len(lows) < 2:
                return {'signal': 'neutral', 'score': 50, 'description': 'ساختار نامشخص'}
            hh = highs[-1] > highs[-2]
            ll = lows[-1] < lows[-2]
            hl = lows[-1] > lows[-2]
            lh = highs[-1] < highs[-2]
            if hh and hl: return {'signal': 'bullish', 'score': 85, 'description': 'ساختار: HH-HL (صعودی قوی)'}
            if ll and lh: return {'signal': 'bearish', 'score': 85, 'description': 'ساختار: LL-LH (نزولی قوی)'}
            if hh: return {'signal': 'bullish', 'score': 70, 'description': 'ساختار: Higher High'}
            if ll: return {'signal': 'bearish', 'score': 70, 'description': 'ساختار: Lower Low'}
            return {'signal': 'neutral', 'score': 50, 'description': 'ساختار: خنثی'}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}'}


# ============================================================
# Price Action
# ============================================================
class PriceAction:
    def detect_patterns(self, df):
        patterns = []
        if len(df) < 5: return patterns
        c, p = df.iloc[-1], df.iloc[-2]
        body_c = abs(c['close'] - c['open'])
        us_c = c['high'] - max(c['close'], c['open'])
        ls_c = min(c['close'], c['open']) - c['low']
        range_c = c['high'] - c['low']
        if range_c == 0: return patterns
        if ls_c > 2 * body_c and ls_c > range_c * 0.6:
            patterns.append({'name_fa': 'پین بار صعودی', 'type': 'bullish', 'description': 'پین بار صعودی', 'strength': 70})
        if us_c > 2 * body_c and us_c > range_c * 0.6:
            patterns.append({'name_fa': 'پین بار نزولی', 'type': 'bearish', 'description': 'پین بار نزولی', 'strength': 70})
        if p['close'] < p['open'] and c['close'] > c['open'] and c['close'] > p['open'] and c['open'] < p['close']:
            patterns.append({'name_fa': 'پوشای صعودی', 'type': 'bullish', 'description': 'پوشای صعودی', 'strength': 75})
        if p['close'] > p['open'] and c['close'] < c['open'] and c['open'] > p['close'] and c['close'] < p['open']:
            patterns.append({'name_fa': 'پوشای نزولی', 'type': 'bearish', 'description': 'پوشای نزولی', 'strength': 75})
        if ls_c > body_c * 2 and us_c < body_c * 0.5 and body_c > 0:
            patterns.append({'name_fa': 'چکش', 'type': 'bullish', 'description': 'چکش - بازگشت از کف', 'strength': 70})
        if us_c > body_c * 2 and ls_c < body_c * 0.5 and body_c > 0:
            patterns.append({'name_fa': 'ستاره دنباله‌دار', 'type': 'bearish', 'description': 'ستاره دنباله‌دار', 'strength': 70})
        return patterns

    def analyze(self, df):
        try:
            patterns = self.detect_patterns(df)
            if not patterns:
                return {'signal': 'neutral', 'score': 50, 'description': 'پرایس اکشن: الگویی نیست', 'patterns': []}
            bull = [p for p in patterns if p['type'] == 'bullish']
            bear = [p for p in patterns if p['type'] == 'bearish']
            if len(bull) > len(bear):
                return {'signal': 'bullish', 'score': np.mean([p['strength'] for p in bull]), 'description': f"پرایس اکشن: {len(bull)} الگوی صعودی", 'patterns': patterns}
            if len(bear) > len(bull):
                return {'signal': 'bearish', 'score': np.mean([p['strength'] for p in bear]), 'description': f"پرایس اکشن: {len(bear)} الگوی نزولی", 'patterns': patterns}
            return {'signal': 'neutral', 'score': 50, 'description': 'پرایس اکشن: متعادل', 'patterns': patterns}
        except Exception as e:
            return {'signal': 'neutral', 'score': 50, 'description': f'خطا: {e}', 'patterns': []}


# ============================================================
# Advanced Analysis Engine
# ============================================================
class AdvancedAnalysisEngine:
    def __init__(self):
        self.smc = SmartMoneyConcepts()
        self.structure = MarketStructure()
        self.price_action = PriceAction()
        self.weights = {'smc': 0.35, 'structure': 0.30, 'price_action': 0.20, 'classic': 0.15}

    def analyze(self, df, classic_signals=None):
        try:
            results = {
                'smc': self.smc.analyze(df),
                'structure': self.structure.analyze(df),
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
# Signal Engine
# ============================================================
class SignalEngine:
    def __init__(self):
        self.fetcher = DataFetcher()
        self.indicators = IndicatorsEngine()
        self.advanced = AdvancedAnalysisEngine()

    def get_trend_direction(self, df):
        if len(df) < EMA_SLOW or 'ema_200' not in df.columns:
            return {"signal": "neutral", "score": 50, "description": "داده کافی نیست"}
        price = df['close'].iloc[-1]
        e21, e50, e200 = df['ema_21'].iloc[-1], df['ema_50'].iloc[-1], df['ema_200'].iloc[-1]
        bull = bear = 0
        if price > e200: bull += 1
        else: bear += 1
        if e21 > e50: bull += 1
        else: bear += 1
        if e50 > e200: bull += 1
        else: bear += 1
        if bull > bear: return {"signal": "bullish", "score": 70, "description": f"روند صعودی ({bull}/3)"}
        if bear > bull: return {"signal": "bearish", "score": 70, "description": f"روند نزولی ({bear}/3)"}
        return {"signal": "neutral", "score": 50, "description": "روند خنثی"}

    def get_rsi_signal(self, df):
        if 'rsi' not in df.columns: return {"signal": "neutral", "score": 50}
        rsi = df['rsi'].iloc[-1]
        if pd.isna(rsi): return {"signal": "neutral", "score": 50}
        if rsi < RSI_OVERSOLD: return {"signal": "bullish", "score": 80, "description": f"RSI: {rsi:.1f} - اشباع فروش"}
        if rsi > RSI_OVERBOUGHT: return {"signal": "bearish", "score": 80, "description": f"RSI: {rsi:.1f} - اشباع خرید"}
        return {"signal": "neutral", "score": 50, "description": f"RSI: {rsi:.1f}"}

    def get_macd_signal(self, df):
        if 'macd' not in df.columns: return {"signal": "neutral", "score": 50}
        mv, sv = df['macd'].iloc[-1], df['macd_signal'].iloc[-1]
        if pd.isna(mv) or pd.isna(sv): return {"signal": "neutral", "score": 50}
        if mv > sv: return {"signal": "bullish", "score": 65, "description": "MACD: صعودی"}
        if mv < sv: return {"signal": "bearish", "score": 65, "description": "MACD: نزولی"}
        return {"signal": "neutral", "score": 50, "description": "MACD: خنثی"}

    def analyze(self):
        try:
            df = self.fetcher.fetch_ohlc(PRIMARY_TIMEFRAME, count=CANDLE_COUNT)
            if df.empty or len(df) < 50:
                return None
            df = self.indicators.calculate_all(df)
            cp = float(df['close'].iloc[-1])

            classic_signals = {
                "trend": self.get_trend_direction(df),
                "rsi": self.get_rsi_signal(df),
                "macd": self.get_macd_signal(df),
            }

            advanced_result = self.advanced.analyze(df, classic_signals)

            signal = {
                'direction': advanced_result['direction'],
                'strength': advanced_result['strength'],
                'confidence': advanced_result.get('confidence', 'low'),
                'bullish_pct': advanced_result.get('bullish_pct', 50),
                'bearish_pct': advanced_result.get('bearish_pct', 50),
                'entry_price': cp,
                'timestamp': datetime.now().isoformat(),
                'timeframe': PRIMARY_TIMEFRAME,
                'results': advanced_result.get('results', {}),
                'classic': classic_signals,
            }

            # محاسبه SL/TP
            atr_val = df['atr'].iloc[-1] if 'atr' in df.columns else cp * 0.002
            if pd.isna(atr_val) or atr_val == 0:
                atr_val = cp * 0.002

            if signal['direction'] == 'BUY':
                signal['stop_loss'] = cp - (atr_val * ATR_SL_MULTIPLIER)
                risk = cp - signal['stop_loss']
                signal['tp1'] = cp + risk
                signal['tp2'] = cp + risk * 2
                signal['tp3'] = cp + risk * 3
            elif signal['direction'] == 'SELL':
                signal['stop_loss'] = cp + (atr_val * ATR_SL_MULTIPLIER)
                risk = signal['stop_loss'] - cp
                signal['tp1'] = cp - risk
                signal['tp2'] = cp - risk * 2
                signal['tp3'] = cp - risk * 3

            return signal

        except Exception as e:
            logger.error(f"خطا در تحلیل: {e}", exc_info=True)
            return None


# ============================================================
# Signal State Manager
# ============================================================
class SignalStateManager:
    """مدیریت وضعیت سیگنال بین اجراها"""

    def __init__(self):
        self.state_file = SIGNAL_STATE_FILE

    def load_state(self):
        """بارگذاری وضعیت قبلی"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    return json.load(f)
        except:
            pass
        return {'last_signal_direction': None, 'last_signal_time': None, 'last_signal_strength': 0}

    def save_state(self, state):
        """ذخیره وضعیت"""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"خطا در ذخیره وضعیت: {e}")

    def should_send_signal(self, signal):
        """بررسی آیا سیگنال باید ارسال شود"""
        state = self.load_state()
        # همیشه ارسال کن (برای تست)
        # در نسخه اصلی فقط در صورت تغییر جهت یا افزایش قدرت
        return True


# ============================================================
# Telegram Sender
# ============================================================
def send_telegram_message(message):
    """ارسال پیام به تلگرام"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=data, timeout=30)
        if response.status_code == 200:
            logger.info("✓ پیام تلگرام ارسال شد")
            return True
        else:
            logger.error(f"خطا در ارسال تلگرام: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"خطا در ارسال تلگرام: {e}")
        return False


def format_signal_message(signal):
    """فرمت‌بندی سیگنال به پیام تلگرام"""
    if signal['direction'] == 'BUY':
        icon, dir_fa, arr = "🟢", "خرید", "▲"
    elif signal['direction'] == 'SELL':
        icon, dir_fa, arr = "🔴", "فروش", "▼"
    else:
        icon, dir_fa, arr = "⏳", "صبر", "●"

    conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(signal['confidence'], "⚪")

    msg = f"{icon} سیگنال {dir_fa} USD/JPY\n"
    msg += f"جهت: {arr} {signal['direction']}\n"
    msg += f"{'='*30}\n\n"
    msg += f"💰 قیمت فعلی: {signal['entry_price']:.3f}\n"
    msg += f"💪 قدرت: {signal['strength']:.0f}%\n"
    msg += f"{conf_icon} اطمینان: {signal['confidence']}\n"
    msg += f"📈 صعودی: {signal['bullish_pct']:.0f}% | 📉 نزولی: {signal['bearish_pct']:.0f}%\n"
    msg += f"⏰ تایم‌فریم: {signal['timeframe']}\n"
    msg += f"🕐 زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    if signal['direction'] in ['BUY', 'SELL']:
        msg += "📌 مشخصات معامله:\n"
        msg += f"🔹 ورود: {signal['entry_price']:.3f}\n"
        if 'stop_loss' in signal:
            msg += f"🛑 حد ضرر: {signal['stop_loss']:.3f} ({abs(signal['entry_price']-signal['stop_loss'])*100:.1f} پیپ)\n"
        if 'tp1' in signal:
            msg += f"🎯 حد سود 1: {signal['tp1']:.3f}\n"
        if 'tp2' in signal:
            msg += f"🎯 حد سود 2: {signal['tp2']:.3f}\n"
        if 'tp3' in signal:
            msg += f"🎯 حد سود 3: {signal['tp3']:.3f}\n"
        msg += "\n"

        # تحلیل پیشرفته
        results = signal.get('results', {})
        msg += "🧠 تحلیل پیشرفته:\n"
        msg += "-"*30 + "\n"
        for method_name in ['smc', 'structure', 'price_action']:
            if method_name in results:
                res = results[method_name]
                if isinstance(res, dict):
                    s = res.get("signal", "neutral")
                    d = res.get("description", "")
                    ic = "✅" if s == "bullish" else "❌" if s == "bearish" else "➖"
                    msg += f"{ic} {d}\n"

        # الگوهای کندلی
        pa = results.get('price_action', {})
        patterns = pa.get('patterns', []) if isinstance(pa, dict) else []
        if patterns:
            msg += "\n🕯 الگوهای کندلی:\n"
            msg += "-"*30 + "\n"
            for p in patterns:
                ic = "🟢" if p["type"] == "bullish" else "🔴" if p["type"] == "bearish" else "⚪"
                msg += f"{ic} {p['name_fa']}\n"
    else:
        msg += "📋 وضعیت فعلی:\n"
        msg += "-"*30 + "\n"
        results = signal.get('results', {})
        for method_name in ['smc', 'structure', 'price_action']:
            if method_name in results:
                res = results[method_name]
                if isinstance(res, dict):
                    msg += f"➖ {res.get('description', '')}\n"

    msg += f"\n{'='*30}\n"
    msg += "⚠️ این سیگنال صرفاً جنبه تحلیلی دارد."
    return msg


# ============================================================
# Main
# ============================================================
def main():
    print("\n" + "=" * 60)
    print("🤖 ربات سیگنال‌دهی USD/JPY - GitHub Actions")
    print("=" * 60)

    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN تنظیم نشده!")
        sys.exit(1)

    if not TELEGRAM_CHAT_ID:
        print("❌ TELEGRAM_CHAT_ID تنظیم نشده!")
        sys.exit(1)

    print(f"📋 توکن: {TELEGRAM_BOT_TOKEN[:15]}...")
    print(f"👤 چت ID: {TELEGRAM_CHAT_ID}")
    print(f"📊 نماد: {SYMBOL_DISPLAY}")
    print(f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # تحلیل بازار
    print("\n🔍 در حال تحلیل بازار...")
    engine = SignalEngine()
    signal = engine.analyze()

    if not signal:
        print("❌ خطا در تحلیل")
        send_telegram_message("❌ خطا در تحلیل بازار. لطفاً بعداً تلاش کنید.")
        return

    print(f"\n📊 نتیجه تحلیل:")
    print(f"جهت: {signal['direction']}")
    print(f"قدرت: {signal['strength']:.0f}%")
    print(f"اطمینان: {signal['confidence']}")
    print(f"صعودی: {signal['bullish_pct']:.0f}% | نزولی: {signal['bearish_pct']:.0f}%")

    # بررسی وضعیت سیگنال قبلی
    state_manager = SignalStateManager()
    should_send = state_manager.should_send_signal(signal)

    if should_send:
        msg = format_signal_message(signal)
        print("\n📱 ارسال به تلگرام...")
        success = send_telegram_message(msg)
        if success:
            print("✅ سیگنال ارسال شد!")
            # ذخیره وضعیت
            state_manager.save_state({
                'last_signal_direction': signal['direction'],
                'last_signal_time': signal['timestamp'],
                'last_signal_strength': signal['strength'],
            })
        else:
            print("❌ خطا در ارسال")
    else:
        print("⏸ سیگنال جدیدی نیست (تکراری)")


if __name__ == "__main__":
    main()

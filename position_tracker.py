# -*- coding: utf-8 -*-
"""
مانیتورینگ پوزیشن و کارنامه هفتگی - نسخه کامل
Position Tracker & Weekly Report - Full Version

ویژگی‌ها:
1. دریافت قیمت لحظه‌ای از OANDA (real-time)
2. مانیتور پوزیشن‌های باز
3. چک TP/SL با قیمت واقعی
4. کارنامه هفتگی خودکار (جمعه‌ها)
5. مدیریت موجودی واقعی

تنظیمات:
- موجودی اولیه: 40 دلار
- اهرم: 20x
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta, timezone

# ============================================================
# Configuration
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")

INITIAL_BALANCE = 40.0
LEVERAGE = 20
RISK_PER_TRADE = 0.02

POSITIONS_DB_FILE = "positions_db.json"

WEEKLY_REPORT_DAY = 4  # جمعه (0=دوشنبه، 4=جمعه)
WEEKLY_REPORT_HOUR = 20  # 20:00 UTC

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================
# Price Fetcher (از OANDA)
# ============================================================
class PriceFetcher:
    """گرفتن قیمت لحظه‌ای از OANDA"""

    BASE_URL = "https://api-fxpractice.oanda.com/v3"
    SYMBOL = "USD_JPY"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {OANDA_API_KEY}",
            "Content-Type": "application/json",
        })

    def get_current_price(self):
        """قیمت لحظه‌ای"""
        try:
            if not OANDA_API_KEY:
                logger.error("OANDA_API_KEY تنظیم نشده")
                return None

            url = f"{self.BASE_URL}/instruments/{self.SYMBOL}/candles"
            params = {"count": 1, "granularity": "S5", "price": "M"}
            response = self.session.get(url, params=params, timeout=15)

            if response.status_code == 200:
                data = response.json()
                if "candles" in data and len(data["candles"]) > 0:
                    return float(data["candles"][-1]["mid"]["c"])
            else:
                logger.error(f"OANDA خطا: {response.status_code}")
        except Exception as e:
            logger.error(f"خطا در قیمت: {e}")
        return None


# ============================================================
# Position Tracker
# ============================================================
class PositionTracker:
    """مانیتورینگ پوزیشن‌های باز و شبیه‌سازی ترید"""

    def __init__(self):
        self.price_fetcher = PriceFetcher()
        self.data = self._load_data()
        self.balance = self.data.get('balance', INITIAL_BALANCE)
        self.telegram_messages = []

    def _load_data(self):
        try:
            if os.path.exists(POSITIONS_DB_FILE):
                with open(POSITIONS_DB_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except:
            pass
        return {
            'balance': INITIAL_BALANCE,
            'positions': [],
            'closed_positions': [],
            'last_report_date': None
        }

    def _save_data(self):
        self.data['balance'] = self.balance
        try:
            with open(POSITIONS_DB_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"خطا در ذخیره: {e}")

    def _send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return False
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
            response = requests.post(url, data=data, timeout=30)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"خطا تلگرام: {e}")
            return False

    def process_signal(self, signal):
        """پردازش سیگنال جدید با قیمت real-time"""
        try:
            # گرفتن قیمت real-time از OANDA
            current_price = self.price_fetcher.get_current_price()

            if current_price is None:
                logger.error("قیمت real-time دریافت نشد")
                return

            logger.info(f"قیمت real-time: {current_price}")

            positions = self.data.get('positions', [])
            closed = self.data.get('closed_positions', [])

            # مرحله 1: چک پوزیشن‌های باز با قیمت real-time
            still_open = []
            for pos in positions:
                result = self._check_position(pos, current_price)
                if result['closed']:
                    closed.append(result['position'])
                    self.balance += result['pnl']
                    self.telegram_messages.append(result['message'])
                    logger.info(f"پوزیشن بسته شد: {result['position']['direction']} - PnL: {result['pnl']:.2f}$")
                else:
                    if result.get('message'):
                        self.telegram_messages.append(result['message'])
                    still_open.append(pos)

            self.data['positions'] = still_open
            self.data['closed_positions'] = closed[-100:]

            # مرحله 2: باز کردن پوزیشن جدید
            if signal.get('direction') in ['BUY', 'SELL'] and signal.get('entry_type') == 'market':
                # بستن پوزیشن‌های مخالف
                opposite = [p for p in still_open if p['direction'] != signal['direction']]
                for pos in opposite:
                    close_result = self._close_position(pos, current_price, 'سیگنال معکوس')
                    self.data['closed_positions'].append(close_result['position'])
                    self.balance += close_result['pnl']
                    self.telegram_messages.append(close_result['message'])
                    if pos in self.data['positions']:
                        self.data['positions'].remove(pos)

                # باز کردن پوزیشن جدید
                same_direction = [p for p in self.data['positions'] if p['direction'] == signal['direction']]
                if not same_direction and signal.get('stop_loss') and signal.get('tp1'):
                    # استفاده از قیمت real-time به‌عنوان قیمت ورود
                    signal_copy = signal.copy()
                    signal_copy['entry_price'] = current_price  # قیمت واقعی فعلی

                    new_pos = self._open_position(signal_copy, current_price)
                    if new_pos:
                        self.data['positions'].append(new_pos)
                        msg = self._format_open_message(new_pos)
                        self.telegram_messages.append(msg)
                        logger.info(f"پوزیشن جدید: {new_pos['direction']} در {current_price}")

            self._save_data()

        except Exception as e:
            logger.error(f"خطا در پردازش: {e}", exc_info=True)

    def _open_position(self, signal, current_price):
        try:
            entry = current_price  # قیمت واقعی فعلی
            sl = signal.get('stop_loss')
            tp1 = signal.get('tp1')
            tp2 = signal.get('tp2')
            tp3 = signal.get('tp3')

            if not sl or not tp1:
                return None

            direction = signal['direction']
            risk_amount = self.balance * RISK_PER_TRADE
            sl_distance = abs(entry - sl)
            sl_pips = sl_distance * 100

            # محاسبه حجم بر اساس ریسک (2% از موجودی)
            pip_value = 6.5  # برای USD/JPY با 1 lot
            size_lots = risk_amount / (sl_pips * pip_value) if sl_pips > 0 else 0.01
            size_lots = max(0.01, min(size_lots, 1.0))
            # حجم دلاری واقعی با اهرم 20x
            size_usd = self.balance * LEVERAGE * size_lots / max(size_lots, 0.01)
            pnl_per_pip = size_lots * pip_value

            return {
                'id': f"POS_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                'direction': direction,
                'entry_price': entry,
                'stop_loss': sl,
                'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
                'size_lots': size_lots,
                'size_usd': size_usd,
                'risk_amount': risk_amount,
                'pnl_per_pip': pnl_per_pip,
                'open_time': datetime.now().isoformat(),
                'setup_type': signal.get('setup_type', 'unknown'),
                'status': 'open',
                'tp1_hit': False,
                'tp2_hit': False,
            }
        except Exception as e:
            logger.error(f"خطا در باز کردن: {e}")
            return None

    def _check_position(self, pos, current_price):
        try:
            direction = pos['direction']
            entry = pos['entry_price']
            sl = pos['stop_loss']
            tp1, tp2, tp3 = pos['tp1'], pos['tp2'], pos['tp3']

            if direction == 'BUY':
                if current_price <= sl:
                    return self._close_position(pos, sl, 'Stop Loss')
                if current_price >= tp3:
                    return self._close_position(pos, tp3, 'TP3 Hit 🎯')
                if current_price >= tp2 and not pos.get('tp2_hit'):
                    pos['tp2_hit'] = True
                    return {'closed': False, 'position': pos, 'pnl': 0,
                            'message': f"📊 TP2 Hit (+{(tp2-entry)*100:.1f} pip) - 30% بسته شد"}
                if current_price >= tp1 and not pos.get('tp1_hit'):
                    pos['tp1_hit'] = True
                    pos['stop_loss'] = entry
                    return {'closed': False, 'position': pos, 'pnl': 0,
                            'message': f"✅ TP1 Hit (+{(tp1-entry)*100:.1f} pip) - SL به ورود منتقل شد"}
            else:
                if current_price >= sl:
                    return self._close_position(pos, sl, 'Stop Loss')
                if current_price <= tp3:
                    return self._close_position(pos, tp3, 'TP3 Hit 🎯')
                if current_price <= tp2 and not pos.get('tp2_hit'):
                    pos['tp2_hit'] = True
                    return {'closed': False, 'position': pos, 'pnl': 0,
                            'message': f"📊 TP2 Hit (+{(entry-tp2)*100:.1f} pip) - 30% بسته شد"}
                if current_price <= tp1 and not pos.get('tp1_hit'):
                    pos['tp1_hit'] = True
                    pos['stop_loss'] = entry
                    return {'closed': False, 'position': pos, 'pnl': 0,
                            'message': f"✅ TP1 Hit (+{(entry-tp1)*100:.1f} pip) - SL به ورود منتقل شد"}

            return {'closed': False, 'position': pos, 'pnl': 0, 'message': None}
        except Exception as e:
            logger.error(f"خطا در چک: {e}")
            return {'closed': False, 'position': pos, 'pnl': 0, 'message': None}

    def _close_position(self, pos, close_price, reason=''):
        try:
            direction = pos['direction']
            entry = pos['entry_price']
            pnl_per_pip = pos.get('pnl_per_pip', 0.065)

            if direction == 'BUY':
                pnl_pips = (close_price - entry) * 100
            else:
                pnl_pips = (entry - close_price) * 100

            pnl = pnl_pips * pnl_per_pip

            pos['close_price'] = close_price
            pos['close_time'] = datetime.now().isoformat()
            pos['close_reason'] = reason
            pos['pnl'] = pnl
            pos['pnl_pips'] = pnl_pips
            pos['status'] = 'closed'

            pnl_emoji = "🟢" if pnl > 0 else "🔴"
            dir_emoji = "🟢" if direction == 'BUY' else "🔴"

            message = (
                f"{pnl_emoji} پوزیشن بسته شد\n"
                f"{'='*30}\n\n"
                f"{dir_emoji} جهت: {direction}\n"
                f"🔹 ورود: {entry:.3f}\n"
                f"🔹 خروج: {close_price:.3f}\n"
                f"📊 سود/ضرر: {pnl_pips:+.1f} پیپ\n"
                f"💰 سود/ضرر: ${pnl:+.2f}\n"
                f"📝 دلیل: {reason}\n"
                f"🕐 زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"💳 موجودی جدید: ${self.balance + pnl:.2f}"
            )

            return {'closed': True, 'position': pos, 'pnl': pnl, 'message': message}
        except Exception as e:
            logger.error(f"خطا در بستن: {e}")
            return {'closed': False, 'position': pos, 'pnl': 0, 'message': None}

    def _format_open_message(self, pos):
        dir_emoji = "🟢" if pos['direction'] == 'BUY' else "🔴"
        return (
            f"📊 پوزیشن باز شد\n"
            f"{'='*30}\n\n"
            f"{dir_emoji} جهت: {pos['direction']}\n"
            f"🔹 ورود: {pos['entry_price']:.3f}\n"
            f"🛑 حد ضرر: {pos['stop_loss']:.3f}\n"
            f"🎯 TP1: {pos['tp1']:.3f}\n"
            f"🎯 TP2: {pos['tp2']:.3f}\n"
            f"🎯 TP3: {pos['tp3']:.3f}\n"
            f"💵 حجم: ${pos['size_usd']:.2f}\n"
            f"🎯 ستاپ: {pos['setup_type']}\n"
            f"💳 موجودی: ${self.balance:.2f}"
        )

    def send_messages(self):
        sent = 0
        for msg in self.telegram_messages:
            if self._send_telegram(msg):
                sent += 1
        self.telegram_messages = []
        return sent

    def check_weekly_report(self):
        try:
            now = datetime.now(timezone.utc)
            # جمعه ساعت 20:00 UTC
            if now.weekday() == WEEKLY_REPORT_DAY and WEEKLY_REPORT_HOUR <= now.hour < WEEKLY_REPORT_HOUR + 1:
                last_report = self.data.get('last_report_date')
                if last_report:
                    try:
                        last_dt = datetime.fromisoformat(last_report.replace('Z', '+00:00'))
                        if (now - last_dt).total_seconds() < 3600:
                            return False
                    except:
                        pass

                report = self._generate_report(now)
                if report:
                    self._send_telegram(report)
                    self.data['last_report_date'] = now.isoformat()
                    self._save_data()
                    return True
            return False
        except:
            return False

    def _generate_report(self, now):
        try:
            closed = self.data.get('closed_positions', [])
            week_ago = now - timedelta(days=7)

            weekly = []
            for pos in closed:
                try:
                    close_time = datetime.fromisoformat(pos.get('close_time', '').replace('Z', '+00:00'))
                    if close_time > week_ago:
                        weekly.append(pos)
                except:
                    continue

            msg = "📊 کارنامه هفتگی ربات\n"
            msg += f"{'='*35}\n"
            msg += f"📅 دوره: 7 روز اخیر\n"
            msg += f"🕐 زمان: {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"

            if not weekly:
                msg += "📭 هیچ معامله‌ای این هفته بسته نشده\n\n"
                msg += "💡 دلایل ممکن:\n"
                msg += "• سیگنال‌ها به TP/SL نرسیدن\n"
                msg += "• بازار رنج بوده\n"
                msg += "• ربات اخیراً فعال شده\n"
                msg += f"\n💳 موجودی فعلی: ${self.balance:.2f}\n"
                msg += f"📂 پوزیشن‌های باز: {len(self.data.get('positions', []))}\n"
                msg += f"\n{'='*35}\n🤖 ربات USD/JPY"
                return msg

            total = len(weekly)
            winning = [p for p in weekly if p.get('pnl', 0) > 0]
            losing = [p for p in weekly if p.get('pnl', 0) <= 0]
            total_pnl = sum(p.get('pnl', 0) for p in weekly)
            total_pips = sum(p.get('pnl_pips', 0) for p in weekly)
            win_rate = (len(winning) / total * 100) if total > 0 else 0
            avg_win = (sum(p.get('pnl', 0) for p in winning) / len(winning)) if winning else 0
            avg_loss = (sum(p.get('pnl', 0) for p in losing) / len(losing)) if losing else 0
            best = max(weekly, key=lambda x: x.get('pnl', 0))
            worst = min(weekly, key=lambda x: x.get('pnl', 0))

            setup_counts = {}
            for p in weekly:
                s = p.get('setup_type', 'unknown')
                setup_counts[s] = setup_counts.get(s, 0) + 1

            roi = ((self.balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100

            msg += "💰 خلاصه مالی:\n"
            msg += f"{'-'*35}\n"
            msg += f"💳 موجودی فعلی: ${self.balance:.2f}\n"
            msg += f"💵 موجودی اولیه: ${INITIAL_BALANCE:.2f}\n"
            pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
            msg += f"{pnl_emoji} سود/ضرر هفته: ${total_pnl:+.2f}\n"
            msg += f"📈 بازدهی (ROI): {roi:+.1f}%\n"
            msg += f"📊 کل پیپ: {total_pips:+.1f}\n\n"

            msg += "📈 آمار معاملات:\n"
            msg += f"{'-'*35}\n"
            msg += f"📊 کل معاملات: {total}\n"
            msg += f"🟢 سودده: {len(winning)}\n"
            msg += f"🔴 ضررده: {len(losing)}\n"
            msg += f"✅ نرخ موفقیت: {win_rate:.1f}%\n"
            msg += f"💵 میانگین سود: ${avg_win:+.2f}\n"
            msg += f"💸 میانگین ضرر: ${avg_loss:+.2f}\n\n"

            if best:
                msg += "🏆 بهترین ترید:\n"
                msg += f"{'-'*35}\n"
                msg += f"• جهت: {best.get('direction')}\n"
                msg += f"• ستاپ: {best.get('setup_type')}\n"
                msg += f"• سود: ${best.get('pnl', 0):+.2f}\n"
                msg += f"• پیپ: {best.get('pnl_pips', 0):+.1f}\n\n"

            if worst:
                msg += "📉 بدترین ترید:\n"
                msg += f"{'-'*35}\n"
                msg += f"• جهت: {worst.get('direction')}\n"
                msg += f"• ستاپ: {worst.get('setup_type')}\n"
                msg += f"• ضرر: ${worst.get('pnl', 0):+.2f}\n"
                msg += f"• پیپ: {worst.get('pnl_pips', 0):+.1f}\n\n"

            if setup_counts:
                msg += "📋 ستاپ‌ها:\n"
                msg += f"{'-'*35}\n"
                for s, c in sorted(setup_counts.items(), key=lambda x: x[1], reverse=True):
                    msg += f"• {s}: {c}\n"
                msg += "\n"

            if win_rate >= 60 and total_pnl > 0:
                eval_text = "🟢 عملکرد عالی!"
            elif win_rate >= 50 and total_pnl > 0:
                eval_text = "🟡 عملکرد خوب"
            elif win_rate >= 40:
                eval_text = "🟠 عملکرد متوسط"
            else:
                eval_text = "🔴 نیاز به بازنگری"

            msg += f"📋 ارزیابی: {eval_text}\n"
            msg += f"\n{'='*35}\n🤖 ربات سیگنال‌دهی USD/JPY"

            return msg
        except Exception as e:
            logger.error(f"خطا در گزارش: {e}")
            return None


# ============================================================
# تابع اصلی - فراخوانی از bot_action.py
# ============================================================
def track_signal(signal, telegram_token=None, telegram_chat_id=None):
    """
    فراخوانی از bot_action.py

    Args:
        signal: سیگنال تولید شده
        telegram_token: توکن تلگرام (اختیاری)
        telegram_chat_id: شناسه چت (اختیاری)
    """
    try:
        # استفاده از متغیرهای سراسری اگه پاس داده نشده
        global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        if telegram_token:
            TELEGRAM_BOT_TOKEN = telegram_token
        if telegram_chat_id:
            TELEGRAM_CHAT_ID = telegram_chat_id

        tracker = PositionTracker()
        tracker.process_signal(signal)
        sent = tracker.send_messages()
        report_sent = tracker.check_weekly_report()

        return {
            'success': True,
            'balance': tracker.balance,
            'open_positions': len(tracker.data.get('positions', [])),
            'messages_sent': sent,
            'report_sent': report_sent,
        }
    except Exception as e:
        logger.error(f"خطا در track_signal: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("📊 تست Position Tracker")
    print("=" * 50)

    # تست قیمت real-time
    print("\n1. تست قیمت real-time از OANDA...")
    pf = PriceFetcher()
    price = pf.get_current_price()
    if price:
        print(f"   ✓ قیمت فعلی: {price}")
    else:
        print("   ✗ خطا - OANDA_API_KEY چک کنید")

    # تست با سیگنال نمونه
    if price:
        print("\n2. تست با سیگنال نمونه...")
        signal = {
            'direction': 'SELL',
            'entry_type': 'market',
            'entry_price': price,
            'current_price': price,
            'stop_loss': price + 0.2,
            'tp1': price - 0.2,
            'tp2': price - 0.4,
            'tp3': price - 0.6,
            'setup_type': 'Test',
            'strength': 70
        }
        result = track_signal(signal)
        print(f"   نتیجه: {result}")

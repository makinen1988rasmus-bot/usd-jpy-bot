# -*- coding: utf-8 -*-
"""
مانیتورینگ پوزیشن و کارنامه هفتگی
Position Tracker & Weekly Report (مستقل)

این فایل کاملاً جدا از bot_action.py کار می‌کنه:
1. فایل last_signal.json رو می‌خونه
2. پوزیشن‌ها رو مدیریت می‌کنه
3. هر جمعه کارنامه هفتگی می‌فرسته

اگه این فایل خراب شه، bot_action.py همچنان کار می‌کنه

تنظیمات:
- موجودی اولیه: 40 دلار
- اهرم: 20x
"""

import os
import sys
import json
import logging
import requests
from datetime import datetime, timedelta, timezone

# ============================================================
# Configuration
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

INITIAL_BALANCE = 40.0
LEVERAGE = 20
RISK_PER_TRADE = 0.02

POSITIONS_DB_FILE = "positions_db.json"
SIGNAL_FILE = "last_signal.json"

WEEKLY_REPORT_DAY = 5  # جمعه
WEEKLY_REPORT_HOUR = 20  # 20:00 UTC

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================
# Data Manager
# ============================================================
class DataManager:
    """مدیریت ذخیره‌سازی داده‌ها"""

    @staticmethod
    def load_json(filename, default=None):
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"خطا در بارگذاری {filename}: {e}")
        return default if default is not None else {}

    @staticmethod
    def save_json(filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"خطا در ذخیره {filename}: {e}")


# ============================================================
# Position Tracker
# ============================================================
class PositionTracker:
    """مانیتورینگ پوزیشن‌های باز و شبیه‌سازی ترید"""

    def __init__(self):
        self.data = DataManager.load_json(POSITIONS_DB_FILE, {
            'balance': INITIAL_BALANCE,
            'positions': [],
            'closed_positions': [],
            'last_report_date': None
        })
        self.balance = self.data.get('balance', INITIAL_BALANCE)
        self.telegram_messages = []

    def save(self):
        self.data['balance'] = self.balance
        DataManager.save_json(POSITIONS_DB_FILE, self.data)

    def process_signal(self, signal):
        """پردازش سیگنال جدید"""
        try:
            current_price = signal.get('current_price', 0)
            if current_price == 0:
                logger.warning("قیمت فعلی موجود نیست")
                return

            positions = self.data.get('positions', [])
            closed = self.data.get('closed_positions', [])

            # ============================================
            # مرحله 1: چک پوزیشن‌های باز
            # ============================================
            still_open = []
            for pos in positions:
                result = self._check_position(pos, current_price)
                if result['closed']:
                    closed.append(result['position'])
                    self.balance += result['pnl']
                    self.telegram_messages.append(result['message'])
                    print(f"   ✓ پوزیشن بسته شد: {result['position']['direction']} - PnL: {result['pnl']:.2f}$")
                else:
                    if result.get('message'):
                        self.telegram_messages.append(result['message'])
                    still_open.append(pos)

            self.data['positions'] = still_open
            self.data['closed_positions'] = closed[-100:]

            # ============================================
            # مرحله 2: باز کردن پوزیشن جدید
            # ============================================
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

                # باز کردن پوزیشن جدید اگه پوزیشن هم‌جهت بازی نباشه
                same_direction = [p for p in self.data['positions'] if p['direction'] == signal['direction']]
                if not same_direction and signal.get('stop_loss') and signal.get('tp1'):
                    new_pos = self._open_position(signal, current_price)
                    if new_pos:
                        self.data['positions'].append(new_pos)
                        print(f"   ✓ پوزیشن جدید: {new_pos['direction']} - حجم: {new_pos['size_usd']:.2f}$")

            self.save()

        except Exception as e:
            logger.error(f"خطا در پردازش: {e}", exc_info=True)

    def _open_position(self, signal, current_price):
        """باز کردن پوزیشن"""
        try:
            entry = signal.get('entry_price', current_price)
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

            # محاسبه حجم (ساده)
            # در USD/JPY، هر پیپ ≈ 6.5$ برای 1 lot
            pip_value = 6.5
            size_lots = risk_amount / (sl_pips * pip_value) if sl_pips > 0 else 0.01
            size_lots = max(0.01, min(size_lots, 1.0))
            size_usd = size_lots * 100000 * (entry / 100)
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
        """چک پوزیشن با قیمت فعلی"""
        try:
            direction = pos['direction']
            entry = pos['entry_price']
            sl = pos['stop_loss']
            tp1 = pos['tp1']
            tp2 = pos['tp2']
            tp3 = pos['tp3']
            pnl_per_pip = pos.get('pnl_per_pip', 0.065)

            if direction == 'BUY':
                # بررسی SL
                if current_price <= sl:
                    return self._close_position(pos, sl, 'Stop Loss')
                # بررسی TP3
                if current_price >= tp3:
                    return self._close_position(pos, tp3, 'TP3 Hit 🎯')
                # بررسی TP2
                if current_price >= tp2 and not pos.get('tp2_hit'):
                    pos['tp2_hit'] = True
                    msg = f"📊 TP2 Hit (+{(tp2 - entry)*100:.1f} pip) - 30% بسته شد"
                    return {'closed': False, 'position': pos, 'pnl': 0, 'message': msg}
                # بررسی TP1
                if current_price >= tp1 and not pos.get('tp1_hit'):
                    pos['tp1_hit'] = True
                    pos['stop_loss'] = entry  # انتقال SL به ورود
                    msg = f"✅ TP1 Hit (+{(tp1 - entry)*100:.1f} pip) - SL به ورود منتقل شد"
                    return {'closed': False, 'position': pos, 'pnl': 0, 'message': msg}
            else:  # SELL
                if current_price >= sl:
                    return self._close_position(pos, sl, 'Stop Loss')
                if current_price <= tp3:
                    return self._close_position(pos, tp3, 'TP3 Hit 🎯')
                if current_price <= tp2 and not pos.get('tp2_hit'):
                    pos['tp2_hit'] = True
                    msg = f"📊 TP2 Hit (+{(entry - tp2)*100:.1f} pip) - 30% بسته شد"
                    return {'closed': False, 'position': pos, 'pnl': 0, 'message': msg}
                if current_price <= tp1 and not pos.get('tp1_hit'):
                    pos['tp1_hit'] = True
                    pos['stop_loss'] = entry
                    msg = f"✅ TP1 Hit (+{(entry - tp1)*100:.1f} pip) - SL به ورود منتقل شد"
                    return {'closed': False, 'position': pos, 'pnl': 0, 'message': msg}

            return {'closed': False, 'position': pos, 'pnl': 0, 'message': None}
        except Exception as e:
            logger.error(f"خطا در چک: {e}")
            return {'closed': False, 'position': pos, 'pnl': 0, 'message': None}

    def _close_position(self, pos, close_price, reason=''):
        """بستن پوزیشن"""
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
                f"💳 موجودی: ${self.balance + pnl:.2f}"
            )

            return {'closed': True, 'position': pos, 'pnl': pnl, 'message': message}
        except Exception as e:
            logger.error(f"خطا در بستن: {e}")
            return {'closed': False, 'position': pos, 'pnl': 0, 'message': None}


# ============================================================
# Weekly Report Generator
# ============================================================
class WeeklyReportGenerator:
    """تولید کارنامه هفتگی"""

    def __init__(self, data):
        self.data = data

    def should_send_report(self):
        """بررسی آیا وقت ارسال گزارش هست"""
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() == WEEKLY_REPORT_DAY and WEEKLY_REPORT_HOUR <= now.hour < WEEKLY_REPORT_HOUR + 1:
                last_report = self.data.get('last_report_date')
                if last_report:
                    try:
                        last_dt = datetime.fromisoformat(last_report.replace('Z', '+00:00'))
                        if (now - last_dt).total_seconds() < 3600:
                            return False
                    except:
                        pass
                return True
            return False
        except:
            return False

    def generate_report(self):
        """تولید گزارش هفتگی"""
        try:
            closed = self.data.get('closed_positions', [])
            now = datetime.now(timezone.utc)
            week_ago = now - timedelta(days=7)

            weekly = []
            for pos in closed:
                try:
                    close_time = datetime.fromisoformat(pos.get('close_time', '').replace('Z', '+00:00'))
                    if close_time > week_ago:
                        weekly.append(pos)
                except:
                    continue

            if not weekly:
                return self._empty_report(now)

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

            balance = self.data.get('balance', INITIAL_BALANCE)
            roi = ((balance - INITIAL_BALANCE) / INITIAL_BALANCE) * 100

            msg = "📊 کارنامه هفتگی ربات\n"
            msg += f"{'='*35}\n"
            msg += f"📅 دوره: 7 روز اخیر\n"
            msg += f"🕐 زمان: {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"

            msg += "💰 خلاصه مالی:\n"
            msg += f"{'-'*35}\n"
            msg += f"💳 موجودی فعلی: ${balance:.2f}\n"
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
                msg += f"• جهت: {best.get('direction', '?')}\n"
                msg += f"• ستاپ: {best.get('setup_type', '?')}\n"
                msg += f"• سود: ${best.get('pnl', 0):+.2f}\n"
                msg += f"• پیپ: {best.get('pnl_pips', 0):+.1f}\n\n"

            if worst:
                msg += "📉 بدترین ترید:\n"
                msg += f"{'-'*35}\n"
                msg += f"• جهت: {worst.get('direction', '?')}\n"
                msg += f"• ستاپ: {worst.get('setup_type', '?')}\n"
                msg += f"• ضرر: ${worst.get('pnl', 0):+.2f}\n"
                msg += f"• پیپ: {worst.get('pnl_pips', 0):+.1f}\n\n"

            if setup_counts:
                msg += "📋 ستاپ‌ها:\n"
                msg += f"{'-'*35}\n"
                for setup, count in sorted(setup_counts.items(), key=lambda x: x[1], reverse=True):
                    msg += f"• {setup}: {count}\n"
                msg += "\n"

            if win_rate >= 60 and total_pnl > 0:
                evaluation = "🟢 عملکرد عالی!"
            elif win_rate >= 50 and total_pnl > 0:
                evaluation = "🟡 عملکرد خوب"
            elif win_rate >= 40:
                evaluation = "🟠 عملکرد متوسط"
            else:
                evaluation = "🔴 نیاز به بازنگری"

            msg += f"📋 ارزیابی: {evaluation}\n"
            msg += f"\n{'='*35}\n"
            msg += "🤖 ربات سیگنال‌دهی USD/JPY"

            self.data['last_report_date'] = now.isoformat()
            return msg
        except Exception as e:
            logger.error(f"خطا در گزارش: {e}")
            return None

    def _empty_report(self, now):
        msg = "📊 کارنامه هفتگی ربات\n"
        msg += f"{'='*35}\n\n"
        msg += f"📅 دوره: 7 روز اخیر\n"
        msg += f"🕐 زمان: {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        msg += "📭 هیچ معامله‌ای بسته نشده\n\n"
        msg += "💡 دلایل ممکن:\n"
        msg += "• سیگنال‌ها به TP/SL نرسیدن\n"
        msg += "• بازار رنج بوده\n"
        msg += f"{'='*35}\n🤖 ربات USD/JPY"
        return msg


# ============================================================
# Telegram Sender
# ============================================================
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"خطا تلگرام: {e}")
        return False


# ============================================================
# Main - نقطه ورود مستقل
# ============================================================
def main():
    print("\n" + "=" * 50)
    print("📊 Position Tracker (مستقل)")
    print("=" * 50)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ تنظیمات تلگرام ناقص!")
        return

    # خواندن سیگنال از فایل
    signal = DataManager.load_json(SIGNAL_FILE, None)
    if not signal:
        print("❌ سیگنالی موجود نیست")
        return

    print(f"📋 سیگنال: {signal.get('direction', 'WAIT')}")
    print(f"💰 قیمت: {signal.get('current_price', 0):.3f}")

    # پردازش سیگنال
    tracker = PositionTracker()
    tracker.process_signal(signal)

    # ارسال پیام‌های پوزیشن
    for msg in tracker.telegram_messages:
        send_telegram_message(msg)

    # بررسی گزارش هفتگی
    report_gen = WeeklyReportGenerator(tracker.data)
    if report_gen.should_send_report():
        print("\n📊 تولید کارنامه هفتگی...")
        report = report_gen.generate_report()
        if report:
            send_telegram_message(report)
            tracker.save()
            print("✓ کارنامه ارسال شد")

    print(f"\n💳 موجودی: ${tracker.balance:.2f}")
    print(f"📂 پوزیشن‌های باز: {len(tracker.data.get('positions', []))}")
    print("✓ کامل شد")


if __name__ == "__main__":
    main()

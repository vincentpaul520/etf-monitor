#!/usr/bin/env python3
"""
510880 红利ETF 云端盯盘脚本
- 从东方财富 API 拉取日K线数据
- 计算 MACD / MA / 成交量均值等技术指标
- 判断5个买入信号
- 通过 QQ 邮箱 SMTP 发送提醒邮件
- 状态持久化到 state.json（由 GitHub Actions 提交回仓库）
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ========== 配置 ==========
SECID = "1.510880"          # 东方财富代码（1=沪市, 0=深市）
SYMBOL = "510880"
NAME = "红利ETF华泰柏瑞"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# QQ 邮箱配置（从 GitHub Secrets 读取）
QQ_EMAIL = os.environ.get("QQ_EMAIL", "")
QQ_SMTP_CODE = os.environ.get("QQ_SMTP_CODE", "")
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# 买入区间参数
BUY_ZONE_LOW = 2.80
BUY_ZONE_HIGH = 2.95
BREAKOUT_LEVEL = 3.10
PULLBACK_LOW = 3.00
PULLBACK_HIGH = 3.05
STOP_LOSS = 2.70

# 股息率阈值（红利ETF 近年每份分红约 0.15-0.16 元）
ANNUAL_DIVIDEND = 0.155  # 预估年度分红（元/份），可手动更新
DIVIDEND_YIELD_THRESHOLD = 5.3  # %

# 状态文件
STATE_FILE = Path(__file__).parent / "state.json"


# ========== 数据获取 ==========
def fetch_kline(days=120):
    """从东方财富拉取日K线数据，返回 list[dict]"""
    end_date = datetime.now().strftime("%Y%m%d")
    beg_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    url = (
        f"{KLINE_API}?secid={SECID}"
        f"&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=0&beg={beg_date}&end={end_date}"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.eastmoney.com"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[ERROR] 获取K线数据失败: {e}")
        return []

    klines_raw = data.get("data", {}).get("klines", [])
    if not klines_raw:
        print("[ERROR] API 返回空数据")
        return []

    # 格式: "date,open,close,high,low,volume,amount,amplitude,chg_pct,chg,turnover"
    result = []
    for line in klines_raw:
        parts = line.split(",")
        if len(parts) < 10:
            continue
        result.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]),
            "chg_pct": float(parts[8]) if parts[8] else 0.0,
        })
    return result


# ========== 技术指标计算 ==========
def calc_ema(values, period):
    """计算 EMA"""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(values[:period]) / period)
    for i in range(period, len(values)):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes, fast=12, slow=26, signal=9):
    """计算 MACD (DIF, DEA, MACD柱)"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = []
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif.append(ema_fast[i] - ema_slow[i])
        else:
            dif.append(None)

    # DEA = EMA(DIF, 9)，只对非 None 的部分算
    dif_valid = [x for x in dif if x is not None]
    dea_valid = calc_ema(dif_valid, signal)

    dea = [None] * len(closes)
    idx = 0
    for i in range(len(closes)):
        if dif[i] is not None:
            dea[i] = dea_valid[idx]
            idx += 1

    macd_hist = []
    for i in range(len(closes)):
        if dif[i] is not None and dea[i] is not None:
            macd_hist.append(2 * (dif[i] - dea[i]))
        else:
            macd_hist.append(None)

    return dif, dea, macd_hist


def calc_ma(values, period):
    """计算简单移动平均"""
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


# ========== 信号判断 ==========
def check_signals(klines):
    """检查5个买入信号，返回 (signals_dict, met_count, details)"""
    if len(klines) < 30:
        return {}, 0, "数据不足，无法判断"

    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]

    latest = klines[-1]
    latest_close = latest["close"]
    latest_date = latest["date"]

    signals = {}
    details = []

    # 信号1: 成交量萎缩
    # 最近5日日均 vs 前20日日均，下降20%以上
    # API返回单位为"手"(1手=100份)，转换为亿份显示
    vol_5 = sum(volumes[-5:]) / 5
    vol_20 = sum(volumes[-20:]) / 20
    vol_shrink = vol_20 > 0 and vol_5 < vol_20 * 0.8
    signals["volume_shrink"] = vol_shrink
    details.append(f"成交量: 近5日均 {vol_5*100/1e8:.2f}亿份 vs 前20日均 {vol_20*100/1e8:.2f}亿份 -> {'✅萎缩' if vol_shrink else '❌未萎缩'}")

    # 信号2: MACD拐头
    dif, dea, macd_hist = calc_macd(closes)
    macd_turning = False
    if macd_hist[-1] is not None and macd_hist[-2] is not None and macd_hist[-3] is not None:
        # 绿柱连续缩短（macd_hist 为负且绝对值缩小）
        if macd_hist[-1] < 0 and macd_hist[-2] < 0:
            if abs(macd_hist[-1]) < abs(macd_hist[-2]):
                # 连续2根缩短
                if abs(macd_hist[-2]) < abs(macd_hist[-3]):
                    macd_turning = True
        # 或者 DIF 从下降转上升
        if not macd_turning and dif[-1] is not None and dif[-2] is not None and dif[-3] is not None:
            if dif[-2] < dif[-3] and dif[-1] > dif[-2]:
                macd_turning = True
    signals["macd_turning"] = macd_turning
    dif_val = dif[-1] if dif[-1] is not None else 0
    dea_val = dea[-1] if dea[-1] is not None else 0
    hist_val = macd_hist[-1] if macd_hist[-1] is not None else 0
    details.append(f"MACD: DIF={dif_val:.4f}, DEA={dea_val:.4f}, 柱={hist_val:.4f} -> {'✅拐头' if macd_turning else '❌空头发散中'}")

    # 信号3: 价格企稳
    ma5 = calc_ma(closes, 5)
    ma5_val = ma5[-1] if ma5[-1] is not None else 0
    above_ma5 = latest_close > ma5_val

    # 连续3日不创新低
    no_new_low = True
    for i in range(-3, 0):
        if lows[i] < lows[i - 1]:
            no_new_low = False
            break

    price_stabilize = above_ma5 or no_new_low
    signals["price_stabilize"] = price_stabilize
    details.append(f"价格: 收盘{latest_close:.3f}, MA5={ma5_val:.3f}, 站上MA5={'是' if above_ma5 else '否'}, 3日不创新低={'是' if no_new_low else '否'} -> {'✅企稳' if price_stabilize else '❌仍在跌'}")

    # 信号4: 股息率偏高
    div_yield = (ANNUAL_DIVIDEND / latest_close) * 100
    div_high = div_yield > DIVIDEND_YIELD_THRESHOLD
    signals["dividend_yield_high"] = div_high
    details.append(f"股息率: {ANNUAL_DIVIDEND}÷{latest_close:.3f}×100 = {div_yield:.2f}% -> {'✅高于阈值' if div_high else '❌低于阈值'}({DIVIDEND_YIELD_THRESHOLD}%)")

    # 信号5: 价格在买入区间
    in_buy_zone = BUY_ZONE_LOW <= latest_close <= BUY_ZONE_HIGH
    in_pullback = False
    # 突破3.10后回踩3.00-3.05
    if len(highs) >= 10:
        recent_high = max(highs[-10:])
        if recent_high >= BREAKOUT_LEVEL and PULLBACK_LOW <= latest_close <= PULLBACK_HIGH:
            in_pullback = True
    price_in_zone = in_buy_zone or in_pullback
    signals["price_in_zone"] = price_in_zone
    zone_desc = f"买入区{BUY_ZONE_LOW}-{BUY_ZONE_HIGH}" if in_buy_zone else (f"回踩区{PULLBACK_LOW}-{PULLBACK_HIGH}" if in_pullback else "不在买入区")
    details.append(f"价格区间: {latest_close:.3f}, {zone_desc} -> {'✅在买入区' if price_in_zone else '❌不在买入区'}")

    met_count = sum(1 for v in signals.values() if v)
    return signals, met_count, "\n".join(details)


# ========== 状态管理 ==========
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "phase": "monitoring",
        "last_check_date": "",
        "last_price": 0,
        "signals": {},
        "signals_met_count": 0,
        "early_warning_sent": False,
        "early_warning_date": "",
        "buy_emails_sent": 0,
        "buy_signal_date": "",
        "buy_signal_confirmed": False,
        "notes": ""
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ========== 邮件发送 ==========
def send_email(subject, body):
    if not QQ_EMAIL or not QQ_SMTP_CODE:
        print(f"[WARN] 邮箱未配置，跳过发送: {subject}")
        print(f"[EMAIL BODY]\n{body}")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = QQ_EMAIL
    msg["To"] = QQ_EMAIL
    msg["Subject"] = subject

    html_body = f"""\


  📈 {subject}


  {body}
  
  ⚠️ 投资有风险，建议仅供参考。本邮件由 GitHub Actions 云端盯盘系统自动发送。

"""
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(QQ_EMAIL, QQ_SMTP_CODE)
            server.sendmail(QQ_EMAIL, QQ_EMAIL, msg.as_string())
        print(f"[OK] 邮件已发送: {subject}")
        return True
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")
        return False


# ========== 主逻辑 ==========
def main():
    print(f"=== 510880 红利ETF 云端盯盘 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    # 1. 获取数据
    klines = fetch_kline(days=180)
    if not klines:
        print("[SKIP] 无法获取数据，跳过本次运行")
        return

    latest = klines[-1]
    today = latest["date"]
    latest_close = latest["close"]
    latest_vol = latest["volume"]
    print(f"最新数据: {today} 收盘={latest_close:.3f} 成交量={latest_vol*100/1e8:.2f}亿份")

    # 2. 读取状态
    state = load_state()

    # 如果今天的数据已经处理过，跳过
    if state["last_check_date"] == today:
        print(f"[SKIP] 今日({today})已检查过，跳过")
        return

    # 3. 判断信号
    signals, met_count, details = check_signals(klines)
    print(f"信号触发: {met_count}/5")
    print(details)

    # 4. 更新状态
    state["last_check_date"] = today
    state["last_price"] = latest_close
    state["signals"] = signals
    state["signals_met_count"] = met_count

    # 5. 止损风险提示
    if latest_close < STOP_LOSS and state["phase"] == "monitoring":
        send_email(
            f"【风险提示】510880跌破止损位 {STOP_LOSS}",
            f"当前价格: {latest_close:.3f}\n止损位: {STOP_LOSS}\n\n价格已跌破止损位，请注意风险！\n\n{details}"
        )

    # 6. 按阶段发送邮件
    if state["phase"] == "monitoring":
        if met_count >= 3 and state["buy_emails_sent"] < 3:
            # 信号确认 → 连发3封
            email_num = state["buy_emails_sent"] + 1
            if email_num == 1:
                subject = f"【信号确认①】510880红利ETF 买入信号初步确认"
                body = f"""当前价格: ¥{latest_close:.3f}
数据日期: {today}

═══ 买入信号确认（{met_count}/5个信号已触发）═══

{details}

═══ 操作建议 ═══
✅ 建议明天准备买入 1/3 仓位
✅ 买入价位: ¥{latest_close:.3f} 附近
✅ 止损位: ¥{STOP_LOSS:.2f}（跌破即出）

剩余资金等后续信号确认再加仓。
信号明细: {json.dumps(signals, ensure_ascii=False)}"""
                send_email(subject, body)
                state["buy_emails_sent"] = 1
                state["buy_signal_date"] = today

            elif email_num == 2:
                subject = f"【准备买入②】510880红利ETF 信号持续，建议今日操作"
                body = f"""当前价格: ¥{latest_close:.3f}
数据日期: {today}

═══ 信号持续确认（{met_count}/5个信号已触发）═══

{details}

═══ 操作建议 ═══
✅ 建议今日买入 1/3 仓位
✅ 买入价位: ¥{latest_close:.3f} 附近
✅ 止损位: ¥{STOP_LOSS:.2f}

剩余 1/3 资金等待第三次确认。
已发邮件: 第{email_num}封/共3封"""
                send_email(subject, body)
                state["buy_emails_sent"] = 2

            elif email_num == 3:
                subject = f"【最佳时机③】510880红利ETF 今日为最佳买入时机"
                body = f"""当前价格: ¥{latest_close:.3f}
数据日期: {today}

═══ 最终确认（{met_count}/5个信号已触发）═══

{details}

═══ 操作建议 ═══
✅ 建议今日完成建仓（最后1/3仓位）
✅ 买入价位: ¥{latest_close:.3f} 附近
✅ 止损位: ¥{STOP_LOSS:.2f}

═══ 长线持有策略 ═══
• 持有期限: 3年以上
• 止损纪律: 跌破¥{STOP_LOSS:.2f} 3日不收回则离场
• 加仓条件: 趋势确认后可适度加仓
• 止盈策略: 分批止盈，先回收成本

这是最后一封提醒邮件。建仓完成后进入持有阶段。"""
                send_email(subject, body)
                state["buy_emails_sent"] = 3
                state["buy_signal_confirmed"] = True
                state["phase"] = "completed"

        elif met_count >= 2 and not state["early_warning_sent"]:
            # 信号逼近 → 发1封预警
            subject = f"【盯盘预警】510880红利ETF 买入信号逼近，请关注"
            body = f"""当前价格: ¥{latest_close:.3f}
数据日期: {today}

═══ 买入信号逼近（{met_count}/5个信号已触发）═══

{details}

═══ 明日关注要点 ═══
• 关注成交量是否继续萎缩
• 关注MACD绿柱是否进一步缩短
• 关注价格是否企稳在¥{BUY_ZONE_LOW:.2f}-{BUY_ZONE_HIGH:.2f}区间

═══ 操作建议 ═══
⏳ 准备资金，明日可能触发买入信号
⏳ 如信号增至3个以上，将开始连发提醒

此为预警邮件，信号确认后将连续3个交易日发送买入提醒。"""
            send_email(subject, body)
            state["early_warning_sent"] = True
            state["early_warning_date"] = today

        else:
            # 仍在监控中
            print(f"[MONITOR] 信号{met_count}/5，继续监控中")

    elif state["phase"] == "completed":
        print("[DONE] 买入信号已完成，不再发邮件")

    # 7. 保存状态
    state["notes"] = f"最近检查: {today}, 价格: {latest_close:.3f}, 信号: {met_count}/5"
    save_state(state)
    print(f"[STATE] 状态已保存: phase={state['phase']}, signals={met_count}/5, early_warning={state['early_warning_sent']}, buy_emails={state['buy_emails_sent']}")


if __name__ == "__main__":
    main()

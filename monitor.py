#!/usr/bin/env python3
"""
510880 红利ETF 云端盯盘脚本
- 从东方财富 API 拉取日K线数据
- 计算 MACD / MA / 成交量均值等技术指标
- 判断5个买入信号
- 通过 QQ 邮箱 SMTP 发送提醒邮件
- 信号组合出问题、买入提醒序列中断、行情拉取失败时发告警
- 状态持久化到 state.json（由 GitHub Actions 提交回仓库）
"""

import html
import http.client
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

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

SIGNAL_NAMES = {
    "volume_shrink": "成交量萎缩",
    "macd_turning": "MACD拐头",
    "price_stabilize": "价格企稳",
    "dividend_yield_high": "股息率偏高",
    "price_in_zone": "价格在买入区",
}

try:
    SH_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    SH_TZ = timezone(timedelta(hours=8))

# 状态文件
STATE_FILE = Path(__file__).parent / "state.json"

DEFAULT_STATE = {
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
    "last_alert_signature": "",
    "last_error_alert_date": "",
    "alert_pending": False,
    "notes": "",
}


def now_sh():
    return datetime.now(SH_TZ)


# ========== 数据获取 ==========
def fetch_kline(days=120, attempts=3, sleeper=None):
    """从东方财富拉取日K线数据，返回 (list[dict], error)。成功时 error 为空字符串。"""
    if sleeper is None:
        sleeper = time.sleep
    end_date = now_sh().strftime("%Y%m%d")
    beg_date = (now_sh() - timedelta(days=days)).strftime("%Y%m%d")
    url = (
        f"{KLINE_API}?secid={SECID}"
        f"&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=0&beg={beg_date}&end={end_date}"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.eastmoney.com"})
    last_err = "未知错误"
    for i in range(attempts):
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, json.JSONDecodeError, TimeoutError, OSError, http.client.HTTPException) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[WARN] 获取K线数据失败 ({i + 1}/{attempts}): {last_err}")
            if i + 1 < attempts:
                sleeper(2 * (i + 1))
            continue

        klines_raw = (data.get("data") or {}).get("klines") or []
        if not klines_raw:
            last_err = "API 返回空数据"
            print(f"[WARN] {last_err} ({i + 1}/{attempts})")
            if i + 1 < attempts:
                sleeper(2 * (i + 1))
            continue

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
        if not result:
            last_err = "K线数据无法解析"
            print(f"[WARN] {last_err} ({i + 1}/{attempts})")
            if i + 1 < attempts:
                sleeper(2 * (i + 1))
            continue
        return result, ""
    return [], last_err


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
    if macd_turning:
        macd_label = "✅拐头"
    elif hist_val < 0:
        macd_label = "❌空头发散中"
    else:
        macd_label = "❌未拐头"
    details.append(f"MACD: DIF={dif_val:.4f}, DEA={dea_val:.4f}, 柱={hist_val:.4f} -> {macd_label}")

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


def failed_signal_names(signals):
    return [name for key, name in SIGNAL_NAMES.items() if not signals.get(key)]


def alert_signature(signals, latest_close):
    parts = [f"{key}:{int(bool(signals.get(key)))}" for key in SIGNAL_NAMES]
    parts.append("stop:" + ("1" if latest_close < STOP_LOSS else "0"))
    return ",".join(parts)


def _signal_changes(prev, signals):
    lines = []
    if not prev:
        return lines
    for key, name in SIGNAL_NAMES.items():
        if key not in prev:
            continue
        old = bool(prev[key])
        new = bool(signals.get(key))
        if old != new:
            lines.append(f"{name}: {'通过' if old else '未通过'} → {'通过' if new else '未通过'}")
    return lines


# ========== 告警决策 ==========
def plan_alerts(state, signals, met_count, details, latest_close, today):
    """决定本次要发的邮件，并返回更新后的状态。不负责真正发送。"""
    state = dict(state)
    prev_signals = dict(state.get("signals") or {})
    state["signals"] = dict(signals)
    messages = []
    routine = False
    stop_sent = False

    if latest_close < STOP_LOSS and state.get("phase") == "monitoring":
        messages.append((
            f"【风险提示】510880跌破止损位 {STOP_LOSS}",
            f"当前价格: {latest_close:.3f}\n止损位: {STOP_LOSS}\n\n价格已跌破止损位，请注意风险！\n\n{details}",
        ))
        stop_sent = True

    if state.get("phase") == "monitoring":
        buy_sent = state.get("buy_emails_sent", 0)
        if met_count >= 3 and buy_sent < 3:
            routine = True
            email_num = buy_sent + 1
            messages.append(_buy_message(email_num, latest_close, today, met_count, details, signals))
            state["buy_emails_sent"] = email_num
            if email_num == 1:
                state["buy_signal_date"] = today
            elif email_num == 3:
                state["buy_signal_confirmed"] = True
                state["phase"] = "completed"
        elif met_count >= 2 and not state.get("early_warning_sent"):
            routine = True
            messages.append(_early_warning_message(latest_close, today, met_count, details))
            state["early_warning_sent"] = True
            state["early_warning_date"] = today
        elif 0 < buy_sent < 3 and met_count < 3:
            routine = True
            sent_date = state.get("buy_signal_date") or "未知日期"
            early_date = state.get("early_warning_date") or ""
            early_note = f"（上次预警 {early_date}）" if early_date else ""
            if met_count < 2:
                early_line = f"• 预警标记已重置{early_note}，信号再次达到 2/5 会重新发预警"
            else:
                early_line = "• 预警标记保持开启，避免 2/5 时再发一封重复预警"
            failed = "、".join(failed_signal_names(signals)) or "无"
            messages.append((
                f"【盯盘告警】510880红利ETF {met_count}/5有问题，买入提醒已中断",
                f"""当前价格: ¥{latest_close:.3f}
数据日期: {today}

未通过: {failed}（{met_count}/5 已触发）

{details}

买入提醒此前停在第 {buy_sent} 封（{sent_date} 发出）。信号没有连续 3 个交易日维持在 3/5 以上，后面的邮件没有再发，所以这段时间检查出问题也不会通知你。

已重新打开提醒:
• 买入邮件计数已从 {buy_sent} 清零
{early_line}
• 信号达到 3/5 会从第 1 封重新连发
• 行情拉取失败会另发故障邮件
• 同一组检查结果不重复发送，结果变化时会再告警""",
            ))
            state["buy_emails_sent"] = 0
            state["buy_signal_date"] = ""
            state["buy_signal_confirmed"] = False

        if met_count < 2:
            state["early_warning_sent"] = False
            state["early_warning_date"] = ""

        if not routine and not stop_sent:
            sig = alert_signature(signals, latest_close)
            failed_names = failed_signal_names(signals)
            if failed_names and sig != (state.get("last_alert_signature") or ""):
                change_lines = _signal_changes(prev_signals, signals)
                change_block = ""
                if change_lines:
                    change_block = "相对上次的变化:\n" + "\n".join(f"• {line}" for line in change_lines) + "\n\n"
                messages.append((
                    f"【盯盘告警】510880红利ETF 信号{met_count}/5，{len(failed_names)}项未通过",
                    f"""当前价格: ¥{latest_close:.3f}
数据日期: {today}

未通过: {"、".join(failed_names)}（{met_count}/5 已触发）

{change_block}{details}

这封是问题告警。同一组检查结果不会每天重复发送；检查结果变化，或行情拉取失败时，会再发邮件。""",
                ))

        state["last_alert_signature"] = alert_signature(signals, latest_close)

    state["last_check_date"] = today
    state["last_price"] = latest_close
    state["signals_met_count"] = met_count
    state["alert_pending"] = False
    state["notes"] = f"最近检查: {today}, 价格: {latest_close:.3f}, 信号: {met_count}/5"
    return messages, state


def _buy_message(email_num, latest_close, today, met_count, details, signals):
    if email_num == 1:
        subject = "【信号确认①】510880红利ETF 买入信号初步确认"
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
    elif email_num == 2:
        subject = "【准备买入②】510880红利ETF 信号持续，建议今日操作"
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
    else:
        subject = "【最佳时机③】510880红利ETF 今日为最佳买入时机"
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
    return subject, body


def _early_warning_message(latest_close, today, met_count, details):
    subject = "【盯盘预警】510880红利ETF 买入信号逼近，请关注"
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
    return subject, body


# ========== 状态管理 ==========
def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            data = None
        if isinstance(data, dict):
            merged = dict(DEFAULT_STATE)
            merged.update(data)
            return merged
    return dict(DEFAULT_STATE)


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

    safe = html.escape(body)
    html_body = f"""<pre style="font-family:sans-serif;white-space:pre-wrap">{safe}</pre>
<p style="color:#666">投资有风险，建议仅供参考。本邮件由 GitHub Actions 云端盯盘系统自动发送。</p>"""
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(QQ_EMAIL, QQ_SMTP_CODE)
            server.sendmail(QQ_EMAIL, QQ_EMAIL, msg.as_string())
        print(f"[OK] 邮件已发送: {subject}")
        return True
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")
        return False


def _notify_fetch_failure(err):
    state = load_state()
    wall = now_sh().strftime("%Y-%m-%d %H:%M")
    day = wall[:10]
    print(f"[ERROR] 无法获取数据: {err}")
    if state.get("last_error_alert_date") == day:
        print("[SKIP] 今天已发过行情失败告警")
        return
    sent = send_email(
        "【盯盘故障】510880 行情拉取失败",
        f"检查时间: {wall}（北京时间）\n错误: {err}\n\n已重试仍失败，这次没有做信号判断，也没有更新买卖状态。请查看 GitHub Actions 日志。",
    )
    if sent or not (QQ_EMAIL and QQ_SMTP_CODE):
        state["last_error_alert_date"] = day
        state["notes"] = f"行情拉取失败: {wall} {err}"
        save_state(state)


def _dispatch(messages, state, new_state):
    if not messages:
        save_state(new_state)
        print(f"[STATE] 状态已保存: phase={new_state['phase']}, signals={new_state['signals_met_count']}/5, alerts=0")
        return

    for subject, body in messages:
        print(f"[ALERT] {subject}")
        if not send_email(subject, body):
            if QQ_EMAIL and QQ_SMTP_CODE:
                state["alert_pending"] = True
                state["notes"] = f"告警发送失败: {new_state['last_check_date']} {subject}"
                save_state(state)
                sys.exit(1)
            print("[WARN] 邮箱未配置，告警只写在日志里，状态仍会推进")
            break
    save_state(new_state)
    print(
        f"[STATE] 状态已保存: phase={new_state['phase']}, signals={new_state['signals_met_count']}/5, "
        f"early_warning={new_state['early_warning_sent']}, buy_emails={new_state['buy_emails_sent']}, alerts={len(messages)}"
    )


# ========== 主逻辑 ==========
def main():
    print(f"=== 510880 红利ETF 云端盯盘 {now_sh().strftime('%Y-%m-%d %H:%M')} 北京时间 ===")

    klines, err = fetch_kline(days=180)
    if not klines:
        _notify_fetch_failure(err)
        sys.exit(1)

    latest = klines[-1]
    today = latest["date"]
    latest_close = latest["close"]
    latest_vol = latest["volume"]
    print(f"最新数据: {today} 收盘={latest_close:.3f} 成交量={latest_vol*100/1e8:.2f}亿份")

    state = load_state()

    if state["last_check_date"] == today and not state.get("alert_pending"):
        print(f"[SKIP] 今日({today})已检查过，跳过")
        return

    signals, met_count, details = check_signals(klines)
    print(f"信号触发: {met_count}/5")
    print(details)

    messages, new_state = plan_alerts(state, signals, met_count, details, latest_close, today)
    if not messages and state.get("phase") == "monitoring":
        print(f"[MONITOR] 信号{met_count}/5，检查结果没有变化，不重复告警")
    _dispatch(messages, state, new_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_email("【盯盘故障】510880 盯盘脚本异常", f"{type(e).__name__}: {e}")
        sys.exit(1)

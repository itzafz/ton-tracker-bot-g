import logging
import asyncio
import aiohttp
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

logging.basicConfig(level=logging.INFO)

# =============================================
BOT_TOKEN = "8616206201:--7lTYyH54bZtt4"
WATCH_ADDRESS = "UQDBYLvK1r4I9ogqJfUyPBZGZTgUNe9e_Ze4Uk42ArOthsqd"
TONCENTER_API = "https://toncenter.com/api/v2"
TONCENTER_API_KEY = ""
OWNER_ID = 6446529683
#=============================================

pending_payments = {}
monitor_tasks = {}
notified_txs = set()

# Track active group chats
active_groups = set()


def nanoton(amount_ton: float) -> int:
    return int(amount_ton * 1_000_000_000)

def make_tonkeeper_link(address: str, amount_ton: float) -> str:
    nano = nanoton(amount_ton)
    return f"https://app.tonkeeper.com/transfer/{address}?amount={nano}"

def ton_from_nano(nano: int) -> float:
    return nano / 1_000_000_000

def make_explorer_link(tx_hash: str) -> str:
    """Tranjection link 🔗"""
    return f"https://tonscan.org/tx/{tx_hash}"


async def check_incoming_payment(watch_address: str, expected_amount: float, start_time: int, tolerance: float = 0.01):
    url = f"{TONCENTER_API}/getTransactions"
    params = {"address": watch_address, "limit": 20}
    headers = {"X-API-Key": TONCENTER_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 401:
                    logging.error("❌ TON Center API key wrong")
                    return None
                data = await resp.json()
                if not data.get("ok"):
                    return None

                txs = data.get("result", [])
                for tx in txs:
                    tx_hash = tx.get("transaction_id", {}).get("hash", "")
                    if tx_hash in notified_txs:
                        continue
                    tx_time = tx.get("utime", 0)
                    if tx_time < start_time:
                        continue
                    in_msg = tx.get("in_msg", {})
                    value_nano = int(in_msg.get("value", 0))
                    if value_nano == 0:
                        continue
                    received = ton_from_nano(value_nano)
                    if abs(received - expected_amount) <= tolerance:
                        sender = in_msg.get("source", "Unknown")
                        return {
                            "hash": tx_hash,
                            "amount": received,
                            "sender": sender,
                            "time": tx_time
                        }
    except Exception as e:
        logging.warning(f"TON API error: {e}")
    return None


async def send_payment_notification(app, user_id: int, result: dict, to_address: str):
    """
    Notification
    """
    explorer_link = make_explorer_link(result['hash'])

    # Full notification message
    msg = (
        f"💰 *Payment Received!*\n\n"
        f"✅ Amount: `{result['amount']} TON`\n"
        f"📥 From: `{result['sender']}`\n"
        f"📤 To: `{to_address}`\n\n"
        f"🔗 [View on Transaction Explorer]({explorer_link})\n\n"
        f"_Transaction confirmed on blockchain_ 🎉"
    )

    sent_to = set()

    # 1. Send to the user's chat
    if user_id not in sent_to:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            sent_to.add(user_id)
        except Exception as e:
            logging.warning(f"Failed to send message to user: {e}")

    # 2. Send to all active groups
    for group_id in list(active_groups):
        if group_id not in sent_to:
            try:
                await app.bot.send_message(
                    chat_id=group_id,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                sent_to.add(group_id)
            except Exception as e:
                logging.warning(f"Failed to send message to group {group_id}: {e}")

    # 3. Always DM the owner (if not already sent)
    if OWNER_ID not in sent_to:
        try:
            await app.bot.send_message(
                chat_id=OWNER_ID,
                text=f"👑 *Owner Alert*\n\n" + msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception as e:
            logging.warning(f"Failed to send DM to owner: {e}")


async def monitor_payment(app, user_id: int, expected_amount: float, to_address: str, start_time: int):
    max_checks = 60
    for _ in range(max_checks):
        await asyncio.sleep(10)
        result = await check_incoming_payment(WATCH_ADDRESS, expected_amount, start_time)
        if result:
            notified_txs.add(result["hash"])
            monitor_tasks.pop(user_id, None)
            await send_payment_notification(app, user_id, result, to_address)
            return

    monitor_tasks.pop(user_id, None)
    timeout_msg = (
        f"⏰ *Payment Timeout*\n\n"
        f"10 minutes passed but `{expected_amount} TON` was not received.\n"
        f"Please check Tonkeeper to verify if the payment was already made."
    )
    try:
        await app.bot.send_message(chat_id=user_id, text=timeout_msg, parse_mode="Markdown")
    except:
        pass
    if OWNER_ID != user_id:
        try:
            await app.bot.send_message(chat_id=OWNER_ID, text=f"👑 *Owner Alert*\n\n" + timeout_msg, parse_mode="Markdown")
        except:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    # Track group chat
    if chat.type in ("group", "supergroup"):
        active_groups.add(chat.id)

    await update.message.reply_text(
        "👋 *TON Payment Bot*\n\n"
        "Commands:\n"
        "`/pay <address> <amount>` — Send a payment\n"
        "`/cancel` — Cancel current payment\n\n"
        "Example:\n"
        "`/pay UQAbc...xyz 0.3`\n\n"
        "You will receive a notification once the payment is confirmed.\n",
        parse_mode="Markdown"
    )


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat = update.effective_chat

    # Track group chat
    if chat.type in ("group", "supergroup"):
        active_groups.add(chat.id)

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Invalid format!\n\n"
            "Correct format:\n`/pay <TON_address> <amount>`\n\n"
            "Example: `/pay UQAbc...xyz 0.3`",
            parse_mode="Markdown"
        )
        return

    address = context.args[0]
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number for the amount. Example: `0.3`", parse_mode="Markdown")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.")
        return

    if not address.startswith(("UQ", "EQ", "0:")):
        await update.message.reply_text("❌ Invalid TON address. It should start with UQ... or EQ...")
        return

    pending_payments[user_id] = {"address": address, "amount": amount, "chat_id": chat.id}

    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data="confirm_pay"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")
    ]]

    await update.message.reply_text(
        f"📋 *Payment Details:*\n\n"
        f"📤 To: `{address}`\n"
        f"💎 Amount: `{amount} TON`\n\n"
        f"Do you want to confirm?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "cancel_pay":
        pending_payments.pop(user_id, None)
        await query.edit_message_text("❌ Payment cancelled.")
        return

    if query.data == "confirm_pay":
        payment = pending_payments.get(user_id)
        if not payment:
            await query.edit_message_text("⚠️ No pending payment found. Please use /pay again.")
            return

        address = payment["address"]
        amount = payment["amount"]
        confirm_time = int(time.time())

        tonkeeper_link = make_tonkeeper_link(address, amount)
        keyboard = [[InlineKeyboardButton("💎 Open in Tonkeeper", url=tonkeeper_link)]]

        await query.edit_message_text(
            f"✅ *Payment Ready!*\n\n"
            f"📤 To: `{address}`\n"
            f"💎 Amount: `{amount} TON`\n\n"
            f"👇 Tap the button below → It will open in Tonkeeper\n"
            f"Approve the transaction from your wallet.\n\n"
            f"🔔 *You will be notified once payment is received:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        pending_payments.pop(user_id, None)

        if user_id in monitor_tasks:
            monitor_tasks[user_id].cancel()

        task = asyncio.create_task(
            monitor_payment(context.application, user_id, amount, address, confirm_time)
        )
        monitor_tasks[user_id] = task


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending_payments.pop(user_id, None)
    if user_id in monitor_tasks:
        monitor_tasks[user_id].cancel()
        monitor_tasks.pop(user_id, None)
        await update.message.reply_text("❌ Payment and monitoring both cancelled.")
    else:
        await update.message.reply_text("❌ Payment cancelled.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 TON Bot is running... 🔔 Group + Owner notifications active!")
    app.run_polling()


if __name__ == "__main__":
    main()

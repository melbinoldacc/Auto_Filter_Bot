import datetime
import time
import os
import asyncio
import logging

from pyrogram import Client, filters, enums
from pyrogram.errors.exceptions.bad_request_400 import MessageTooLong
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.users_chats_db import db
from info import ADMINS
from utils import (
    users_broadcast,
    groups_broadcast,
    temp,
    get_readable_time,
    clear_junk,
    junk_group
)


logger = logging.getLogger(__name__)

lock = asyncio.Lock()

# Stores admins who are currently waiting for YES/NO
BROADCAST_WAITING = set()


# ============================================================
# HELPER - SUPPORT BOTH ASYNC CURSOR AND NORMAL LIST
# ============================================================

async def _to_list(items):
    """
    Convert an async cursor or normal iterable into a list.
    """
    if items is None:
        return []

    if hasattr(items, "__aiter__"):
        return [item async for item in items]

    return list(items)


# ============================================================
# BROADCAST PROMPT TIMEOUT
# ============================================================

async def _expire_broadcast_prompt(prompt, admin_id):
    """
    Automatically cancel the YES/NO prompt after 60 seconds.
    """
    try:
        await asyncio.sleep(60)

        if admin_id in BROADCAST_WAITING:
            BROADCAST_WAITING.discard(admin_id)

            try:
                await prompt.edit(
                    "⌛ <b>Timed out.</b>\n\n"
                    "Broadcast cancelled because no response was received.",
                    reply_markup=None
                )
            except Exception:
                pass

    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Error while expiring broadcast prompt")


# ============================================================
# CANCEL BROADCAST
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^broadcast_cancel#(users|groups)#(\d+)$")
)
async def broadcast_cancel(bot, query):

    try:
        _, target, admin_id = query.data.split("#")
        admin_id = int(admin_id)

        # Only the admin who started the broadcast can cancel it
        if query.from_user.id != admin_id:
            return await query.answer(
                "❌ This broadcast belongs to another admin.",
                show_alert=True
            )

        if query.from_user.id not in ADMINS:
            return await query.answer(
                "❌ You are not authorized.",
                show_alert=True
            )

        if target == "users":

            temp.B_USERS_CANCEL = True

            await query.answer(
                "🛑 Cancelling users broadcast..."
            )

            try:
                await query.message.edit(
                    "🛑 <b>Trying to cancel users broadcasting...</b>",
                    reply_markup=None
                )
            except Exception:
                pass

        elif target == "groups":

            temp.B_GROUPS_CANCEL = True

            await query.answer(
                "🛑 Cancelling groups broadcast..."
            )

            try:
                await query.message.edit(
                    "🛑 <b>Trying to cancel groups broadcasting...</b>",
                    reply_markup=None
                )
            except Exception:
                pass

    except Exception:
        logger.exception("Error in broadcast_cancel callback")

        try:
            await query.answer(
                "❌ Something went wrong.",
                show_alert=True
            )
        except Exception:
            pass


# ============================================================
# YES / NO BROADCAST CALLBACK
# ============================================================

@Client.on_callback_query(
    filters.regex(
        r"^broadcast_pin#(users|groups)#(yes|no)#(-?\d+)#(\d+)#(\d+)$"
    )
)
async def broadcast_pin_callback(bot, query):

    try:
        parts = query.data.split("#")

        # Format:
        # broadcast_pin#users#yes#chat_id#message_id#admin_id

        _, target, choice, chat_id, message_id, admin_id = parts

        chat_id = int(chat_id)
        message_id = int(message_id)
        admin_id = int(admin_id)

        # ----------------------------------------------------
        # SECURITY CHECK
        # ----------------------------------------------------

        if query.from_user.id != admin_id:
            return await query.answer(
                "❌ This broadcast belongs to another admin.",
                show_alert=True
            )

        if query.from_user.id not in ADMINS:
            return await query.answer(
                "❌ You are not authorized.",
                show_alert=True
            )

        # ----------------------------------------------------
        # CHECK IF REQUEST IS STILL ACTIVE
        # ----------------------------------------------------

        if admin_id not in BROADCAST_WAITING:
            return await query.answer(
                "⌛ This broadcast request has expired.",
                show_alert=True
            )

        # Remove waiting state
        BROADCAST_WAITING.discard(admin_id)

        # ----------------------------------------------------
        # CHECK BROADCAST LOCK
        # ----------------------------------------------------

        if lock.locked():

            try:
                await query.message.edit(
                    "⚠️ <b>Another broadcast is already in progress.</b>\n\n"
                    "Please wait until it finishes.",
                    reply_markup=None
                )
            except Exception:
                pass

            return await query.answer(
                "Another broadcast is already running.",
                show_alert=True
            )

        # ----------------------------------------------------
        # GET ORIGINAL MESSAGE
        # ----------------------------------------------------

        try:
            b_msg = await bot.get_messages(
                chat_id,
                message_id
            )

        except Exception:

            try:
                await query.message.edit(
                    "❌ <b>Could not find the message to broadcast.</b>",
                    reply_markup=None
                )
            except Exception:
                pass

            return await query.answer(
                "Broadcast message not found.",
                show_alert=True
            )

        if not b_msg:
            try:
                await query.message.edit(
                    "❌ <b>Could not find the message to broadcast.</b>",
                    reply_markup=None
                )
            except Exception:
                pass

            return await query.answer(
                "Broadcast message not found.",
                show_alert=True
            )

        is_pin = choice == "yes"

        # ----------------------------------------------------
        # REMOVE BUTTONS / SHOW STARTING MESSAGE
        # ----------------------------------------------------

        try:

            if target == "users":

                await query.message.edit(
                    "📤 <b>Starting users broadcast...</b>\n\n"
                    f"📌 Pin message: <b>{'YES' if is_pin else 'NO'}</b>",
                    reply_markup=None
                )

            else:

                await query.message.edit(
                    "📤 <b>Starting groups broadcast...</b>\n\n"
                    f"📌 Pin message: <b>{'YES' if is_pin else 'NO'}</b>",
                    reply_markup=None
                )

        except Exception:
            pass

        await query.answer(
            "Broadcast started."
        )

        # ----------------------------------------------------
        # USERS BROADCAST
        # ----------------------------------------------------

        if target == "users":

            # Reset cancellation flag
            temp.B_USERS_CANCEL = False

            users = await _to_list(
                await db.get_all_users()
            )

            total_users = len(users)

            if total_users == 0:

                return await query.message.reply(
                    "❌ <b>No users found for broadcast.</b>",
                    parse_mode=enums.ParseMode.HTML
                )

            status_msg = await query.message.reply_text(
                "📤 <b>Broadcasting your message...</b>",
                parse_mode=enums.ParseMode.HTML
            )

            success = 0
            blocked = 0
            deleted = 0
            failed = 0

            start_time = time.time()
            cancelled = False

            async def send_user(user):

                try:

                    _, result = await users_broadcast(
                        bot,
                        int(user["id"]),
                        b_msg,
                        is_pin
                    )

                    return result

                except Exception:

                    logger.exception(
                        "Error sending broadcast to user %s",
                        user.get("id")
                    )

                    return "Error"

            # ------------------------------------------------
            # LOCK BROADCAST
            # ------------------------------------------------

            async with lock:

                for i in range(0, total_users, 100):

                    # Check cancellation
                    if temp.B_USERS_CANCEL:

                        temp.B_USERS_CANCEL = False
                        cancelled = True
                        break

                    batch = users[i:i + 100]

                    results = await asyncio.gather(
                        *[
                            send_user(user)
                            for user in batch
                        ]
                    )

                    # Count results
                    for result in results:

                        if result == "Success":
                            success += 1

                        elif result == "Blocked":
                            blocked += 1

                        elif result == "Deleted":
                            deleted += 1

                        elif result == "Error":
                            failed += 1

                    done = i + len(batch)

                    elapsed = get_readable_time(
                        time.time() - start_time
                    )

                    # Cancel button
                    cancel_button = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "❌ CANCEL",
                                    callback_data=(
                                        f"broadcast_cancel#users#{admin_id}"
                                    )
                                )
                            ]
                        ]
                    )

                    try:

                        await status_msg.edit(
                            f"📣 <b>Broadcast Progress:</b>\n\n"
                            f"👥 Total: <code>{total_users}</code>\n"
                            f"✅ Done: <code>{done}</code>\n"
                            f"📬 Success: <code>{success}</code>\n"
                            f"⛔ Blocked: <code>{blocked}</code>\n"
                            f"🗑️ Deleted: <code>{deleted}</code>\n"
                            f"❌ Failed: <code>{failed}</code>\n"
                            f"⏱️ Time: {elapsed}",
                            reply_markup=cancel_button
                        )

                    except Exception:
                        pass

                    await asyncio.sleep(0.1)

            # ------------------------------------------------
            # FINAL STATUS
            # ------------------------------------------------

            elapsed = get_readable_time(
                time.time() - start_time
            )

            final_status = (
                f"{'❌ <b>Broadcast Cancelled.</b>' if cancelled else '✅ <b>Broadcast Completed.</b>'}\n\n"
                f"🕒 Time: {elapsed}\n"
                f"👥 Total: <code>{total_users}</code>\n"
                f"📬 Success: <code>{success}</code>\n"
                f"⛔ Blocked: <code>{blocked}</code>\n"
                f"🗑️ Deleted: <code>{deleted}</code>\n"
                f"❌ Failed: <code>{failed}</code>"
            )

            try:

                await status_msg.edit(
                    final_status,
                    reply_markup=None
                )

            except Exception:
                pass

        # ----------------------------------------------------
        # GROUPS BROADCAST
        # ----------------------------------------------------

        elif target == "groups":

            # Reset cancellation flag
            temp.B_GROUPS_CANCEL = False

            chats = await _to_list(
                await db.get_all_chats()
            )

            total_chats = len(chats)

            if total_chats == 0:

                return await query.message.reply(
                    "❌ <b>No groups found for broadcast.</b>",
                    parse_mode=enums.ParseMode.HTML
                )

            status_msg = await query.message.reply_text(
                "📤 <b>Broadcasting your message to groups...</b>",
                parse_mode=enums.ParseMode.HTML
            )

            success = 0
            failed = 0
            done = 0

            start_time = time.time()
            cancelled = False

            # ------------------------------------------------
            # LOCK BROADCAST
            # ------------------------------------------------

            async with lock:

                for chat in chats:

                    # Check cancellation
                    if temp.B_GROUPS_CANCEL:

                        temp.B_GROUPS_CANCEL = False
                        cancelled = True
                        break

                    try:

                        sts = await groups_broadcast(
                            int(chat["id"]),
                            b_msg,
                            is_pin
                        )

                    except Exception:

                        logger.exception(
                            "Error broadcasting to group %s",
                            chat.get("id")
                        )

                        sts = "Error"

                    if sts == "Success":
                        success += 1
                    else:
                        failed += 1

                    done += 1

                    # Update every 10 groups
                    if done % 10 == 0 or done == total_chats:

                        time_taken = get_readable_time(
                            time.time() - start_time
                        )

                        cancel_button = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "❌ CANCEL",
                                        callback_data=(
                                            f"broadcast_cancel#groups#{admin_id}"
                                        )
                                    )
                                ]
                            ]
                        )

                        try:

                            await status_msg.edit(
                                f"📣 <b>Group Broadcast Progress:</b>\n\n"
                                f"👥 Total Groups: <code>{total_chats}</code>\n"
                                f"✅ Completed: <code>{done} / {total_chats}</code>\n"
                                f"📬 Success: <code>{success}</code>\n"
                                f"❌ Failed: <code>{failed}</code>\n"
                                f"⏱️ Time: {time_taken}",
                                reply_markup=cancel_button
                            )

                        except Exception:
                            pass

            # ------------------------------------------------
            # FINAL GROUP STATUS
            # ------------------------------------------------

            time_taken = get_readable_time(
                time.time() - start_time
            )

            final_status = (
                f"{'❌ <b>Groups broadcast cancelled!</b>' if cancelled else '✅ <b>Group broadcast completed.</b>'}\n\n"
                f"⏱️ Completed in {time_taken}\n\n"
                f"👥 Total Groups: <code>{total_chats}</code>\n"
                f"✅ Completed: <code>{done} / {total_chats}</code>\n"
                f"📬 Success: <code>{success}</code>\n"
                f"❌ Failed: <code>{failed}</code>"
            )

            try:

                await status_msg.edit(
                    final_status,
                    reply_markup=None
                )

            except MessageTooLong:

                with open("reason.txt", "w+", encoding="utf-8") as outfile:

                    outfile.write(
                        f"Total Groups: {total_chats}\n"
                        f"Completed: {done}\n"
                        f"Success: {success}\n"
                        f"Failed: {failed}\n"
                    )

                await query.message.reply_document(
                    "reason.txt",
                    caption=final_status
                )

                try:
                    os.remove("reason.txt")
                except Exception:
                    pass

            except Exception:
                pass

    except Exception:

        logger.exception(
            "Error processing broadcast callback"
        )

        BROADCAST_WAITING.discard(admin_id)

        try:

            await query.message.reply(
                "❌ <b>An unexpected error occurred while starting the broadcast.</b>",
                parse_mode=enums.ParseMode.HTML
            )

        except Exception:
            pass

        try:
            await query.answer(
                "❌ Broadcast failed.",
                show_alert=True
            )
        except Exception:
            pass


# ============================================================
# /broadcast - USERS
# ============================================================

@Client.on_message(
    filters.command("broadcast")
    & filters.user(ADMINS)
    & filters.private
)
async def broadcast_users(bot, message):

    # --------------------------------------------------------
    # MUST REPLY TO A MESSAGE
    # --------------------------------------------------------

    if not message.reply_to_message:

        return await message.reply(
            "<b>Reply to a message to broadcast.</b>",
            parse_mode=enums.ParseMode.HTML
        )

    # --------------------------------------------------------
    # CHECK ACTIVE BROADCAST
    # --------------------------------------------------------

    if lock.locked():

        return await message.reply(
            "⚠️ <b>Another broadcast is in progress.</b>\n\n"
            "Please wait until it finishes.",
            parse_mode=enums.ParseMode.HTML
        )

    admin_id = message.from_user.id

    # Prevent multiple waiting prompts
    if admin_id in BROADCAST_WAITING:

        return await message.reply(
            "⚠️ <b>You already have a broadcast waiting for YES/NO.</b>\n\n"
            "Please answer the existing prompt first.",
            parse_mode=enums.ParseMode.HTML
        )

    BROADCAST_WAITING.add(admin_id)

    # --------------------------------------------------------
    # INLINE YES / NO BUTTONS
    # --------------------------------------------------------

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ YES",
                    callback_data=(
                        f"broadcast_pin#users#yes#"
                        f"{message.chat.id}#"
                        f"{message.reply_to_message.id}#"
                        f"{admin_id}"
                    )
                ),
                InlineKeyboardButton(
                    "❌ NO",
                    callback_data=(
                        f"broadcast_pin#users#no#"
                        f"{message.chat.id}#"
                        f"{message.reply_to_message.id}#"
                        f"{admin_id}"
                    )
                )
            ]
        ]
    )

    try:

        ask = await message.reply(
            "<b>Do you want to pin this message in users?</b>\n\n"
            "Choose an option below:",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard
        )

        # Start 60-second timeout
        asyncio.create_task(
            _expire_broadcast_prompt(
                ask,
                admin_id
            )
        )

    except Exception:

        BROADCAST_WAITING.discard(admin_id)

        logger.exception(
            "Error creating users broadcast prompt"
        )

        return await message.reply(
            "❌ Failed to create broadcast options."
        )


# ============================================================
# /grp_broadcast - GROUPS
# ============================================================

@Client.on_message(
    filters.command("grp_broadcast")
    & filters.user(ADMINS)
    & filters.private
)
async def broadcast_group(bot, message):

    # --------------------------------------------------------
    # MUST REPLY TO A MESSAGE
    # --------------------------------------------------------

    if not message.reply_to_message:

        return await message.reply(
            "<b>Reply to a message to group broadcast.</b>",
            parse_mode=enums.ParseMode.HTML
        )

    # --------------------------------------------------------
    # CHECK ACTIVE BROADCAST
    # --------------------------------------------------------

    if lock.locked():

        return await message.reply(
            "⚠️ <b>Another broadcast is in progress.</b>\n\n"
            "Please wait until it finishes.",
            parse_mode=enums.ParseMode.HTML
        )

    admin_id = message.from_user.id

    # Prevent multiple waiting prompts
    if admin_id in BROADCAST_WAITING:

        return await message.reply(
            "⚠️ <b>You already have a broadcast waiting for YES/NO.</b>\n\n"
            "Please answer the existing prompt first.",
            parse_mode=enums.ParseMode.HTML
        )

    BROADCAST_WAITING.add(admin_id)

    # --------------------------------------------------------
    # INLINE YES / NO BUTTONS
    # --------------------------------------------------------

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ YES",
                    callback_data=(
                        f"broadcast_pin#groups#yes#"
                        f"{message.chat.id}#"
                        f"{message.reply_to_message.id}#"
                        f"{admin_id}"
                    )
                ),
                InlineKeyboardButton(
                    "❌ NO",
                    callback_data=(
                        f"broadcast_pin#groups#no#"
                        f"{message.chat.id}#"
                        f"{message.reply_to_message.id}#"
                        f"{admin_id}"
                    )
                )
            ]
        ]
    )

    try:

        ask = await message.reply(
            "<b>Do you want to pin this message in groups?</b>\n\n"
            "Choose an option below:",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard
        )

        # Start 60-second timeout
        asyncio.create_task(
            _expire_broadcast_prompt(
                ask,
                admin_id
            )
        )

    except Exception:

        BROADCAST_WAITING.discard(admin_id)

        logger.exception(
            "Error creating groups broadcast prompt"
        )

        return await message.reply(
            "❌ Failed to create broadcast options."
        )


# ============================================================
# /clear_junk
# ============================================================

@Client.on_message(
    filters.command("clear_junk")
    & filters.user(ADMINS)
)
async def remove_junkuser__db(bot, message):

    users = await db.get_all_users()

    b_msg = message

    sts = await message.reply_text(
        "ɪɴ ᴘʀᴏɢʀᴇss.... ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ"
    )

    start_time = time.time()

    total_users = await db.total_users_count()

    blocked = 0
    deleted = 0
    failed = 0
    done = 0

    async for user in users:

        pti, sh = await clear_junk(
            int(user["id"]),
            b_msg
        )

        if not pti:

            if sh == "Blocked":
                blocked += 1

            elif sh == "Deleted":
                deleted += 1

            elif sh == "Error":
                failed += 1

        done += 1

        if not done % 50:

            await sts.edit(
                f"In Progress:\n\n"
                f"Total Users {total_users}\n"
                f"Completed: {done} / {total_users}\n"
                f"Blocked: {blocked}\n"
                f"Deleted: {deleted}"
            )

    time_taken = datetime.timedelta(
        seconds=int(
            time.time() - start_time
        )
    )

    await sts.delete()

    await bot.send_message(
        message.chat.id,
        f"Completed:\n"
        f"Completed in {time_taken} seconds.\n\n"
        f"Total Users {total_users}\n"
        f"Completed: {done} / {total_users}\n"
        f"Blocked: {blocked}\n"
        f"Deleted: {deleted}"
    )


# ============================================================
# /junk_group / /clear_junk_group
# ============================================================

@Client.on_message(
    filters.command(
        ["junk_group", "clear_junk_group"]
    )
    & filters.user(ADMINS)
)
async def junk_clear_group(bot, message):

    groups = await db.get_all_chats()

    if not groups:

        grp = await message.reply_text(
            "❌ Nᴏ ɢʀᴏᴜᴘs ғᴏᴜɴᴅ ғᴏʀ ᴄʟᴇᴀʀ Jᴜɴᴋ ɢʀᴏᴜᴘs."
        )

        await asyncio.sleep(60)

        await grp.delete()

        return

    b_msg = message

    sts = await message.reply_text(
        text=".............."
    )

    start_time = time.time()

    total_groups = await db.total_chat_count()

    done = 0
    failed = ""
    deleted = 0

    async for group in groups:

        pti, sh, ex = await junk_group(
            int(group["id"]),
            b_msg
        )

        if not pti:

            if sh == "deleted":

                deleted += 1
                failed += ex

                try:

                    await bot.leave_chat(
                        int(group["id"])
                    )

                except Exception as e:

                    logger.warning(
                        "%s > %s",
                        e,
                        group["id"]
                    )

        done += 1

        if not done % 50:

            await sts.edit(
                f"in progress:\n\n"
                f"Total Groups {total_groups}\n"
                f"Completed: {done} / {total_groups}\n"
                f"Deleted: {deleted}"
            )

    time_taken = datetime.timedelta(
        seconds=int(
            time.time() - start_time
        )
    )

    await sts.delete()

    try:

        await bot.send_message(
            message.chat.id,
            f"Completed:\n"
            f"Completed in {time_taken} seconds.\n\n"
            f"Total Groups {total_groups}\n"
            f"Completed: {done} / {total_groups}\n"
            f"Deleted: {deleted}\n\n"
            f"Filed Reson:- {failed}"
        )

    except MessageTooLong:

        with open(
            "junk.txt",
            "w+",
            encoding="utf-8"
        ) as outfile:

            outfile.write(failed)

        await message.reply_document(
            "junk.txt",
            caption=(
                f"Completed:\n"
                f"Completed in {time_taken} seconds.\n\n"
                f"Total Groups {total_groups}\n"
                f"Completed: {done} / {total_groups}\n"
                f"Deleted: {deleted}"
            )
        )

        try:
            os.remove("junk.txt")
        except Exception:
            pass

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, Unauthorized
from google_sheet_to_json import fetch
from analytics import Analytics
import json
import os
import ast
from time import sleep,time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pytz import timezone

# How long the container exist
LIFESPAN = 7200

IST = timezone("Asia/Kolkata")

import logging

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

DATA_UPDATE_MIN = 1
SCHEDULE_MSG_MIN = 60
SCHEDULE_CHANNEL = os.environ["SCHEDULE_CHANNEL"]
try:
    BIN_CHANNEL = os.environ["BIN_CHANNEL"]
except:
    BIN_CHANNEL = None
    logging.warning("No Bin. Won't Bin")
BIN_MAX_LENGTH = 3000

# Save the SHEET_SECRET from environment
# if the KEYFILE is not available
KEYFILE = "./credentials/service-account.json"
if not os.path.isfile(KEYFILE):
    try:
        SHEET_SECRET = os.environ["SHEET_SECRET"]
    except KeyError:
        logging.error("Sheet secret not available. Fail!")

    with open("./credentials/service-account.json", "w") as f:
        json.dump(SHEET_SECRET, f)


def clean_data(data):
    """
    Only choose the necessary columns
    """
    data = pd.DataFrame(data)
    sel_cols = [
        "hospitalname",
        "zone",
        "pincode",
        "contactno",
        "general",
        "hdu",
        "icu",
        "icu-v",
        "remarks",
        "timestamp",
        "type",
        "interested",
    ]
    col_maps = {
        "hospitalname": "hospital",
        "contactno": "phonenumber",
        "icu-v": "icuwithventilator",
    }
    int_cols = ["general", "hdu", "icu", "icuwithventilator", "timestamp"]

    data = data[sel_cols]
    data.rename(columns=col_maps, inplace=True)
    data[int_cols] = data[int_cols].apply(lambda x: x.replace("-", "0"))
    # Interest condition
    data = data[data["interested"].str.contains("Yes")]
    # Type condition
    data = data[(data["type"] == "Covid") | (data["type"] == "Both")]

    return data


def read_status_logs():
    """
    Check metadata file to check the freshness
    If the freshness is less than a minute, fetch the data again

    """
    try:
        with open("metadata.json", "r") as f:
            meta = json.load(f)
            last_updated_time = datetime.strptime(
                meta["last_updated_time"], "%Y-%m-%d %H:%M:%S%z"
            )

    except Exception as e:
        logging.error(e)
        logging.info("Will create a new metadata file")
        meta = {}
        TIME_START = "1900-01-01 00:00:00+05:30"
        meta["scheduled_sent_time"] = meta["last_updated_time"] = TIME_START
        last_updated_time = datetime.strptime(TIME_START, "%Y-%m-%d %H:%M:%S%z")

    if (datetime.now(IST) - last_updated_time) > timedelta(minutes=DATA_UPDATE_MIN):
        fetch_start_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S%z")
        try:
            newData = fetch()
            logging.info("Data refreshed")
        except Exception as e:
            logging.error(e)
            return None
        with open("metadata.json", "w") as f:
            nD = pd.DataFrame(newData)
            meta.update(
                {
                    "last_updated_time": fetch_start_time,
                    "zones": sorted([z for z in list(nD["zone"].unique()) if z != ""]),
                    "pincodes": sorted(
                        [z for z in list(nD["pincode"].unique()) if z != ""]
                    ),
                }
            )
            json.dump(meta, f, indent=4)

    try:
        with open("output.json", "r") as f:
            status = json.load(f)
            status = pd.DataFrame(status)
            # Clean
            status = clean_data(status)
        return status
    except FileNotFoundError:
        logging.info("Output file does not exist and couldn't be fetched!")
        return None


def hosps_in_pincode(status, pincode):
    """
    Return the data of all hospitals in a pincode
    Also returns the count of hospitals
    """
    pincode = str(pincode)
    sel_status = status[status["pincode"] == pincode]

    try:
        hosp_count = sel_status.hospital.nunique()
    except KeyError:
        hosp_count = 0

    return sel_status, hosp_count


def hosps_in_zone(status, zone):
    """
    Return the data of all hospitals in a pincode
    Also returns the count of hospitals
    """
    sel_status = status[status["zone"] == zone]

    try:
        hosp_count = sel_status.hospital.nunique()
    except KeyError:
        hosp_count = 0
    return sel_status, hosp_count


def hosps_in_bedtype(status, bedtype):
    """
    Return the data of all hospitals that has available
    beds in the provided bedtype
    Also returns the count of hospitals
    """
    sel_status = status[(pd.to_numeric(status[bedtype]) > 0)]

    try:
        hosp_count = sel_status.hospital.nunique()
    except KeyError:
        hosp_count = 0
    return sel_status, hosp_count


def get_latest(s, n_latest=1):
    """
    For each hospital, get the `n_latest` status logs
    """
    s.sort_values("timestamp", ascending=False, inplace=True)
    result = (
        s[
            [
                "timestamp",
                "general",
                "hdu",
                "icu",
                "icuwithventilator",
                "phonenumber",
                "remarks",
            ]
        ]
        .head(n_latest)
        .to_dict("records")
    )

    return result


def prepare_message(logs, header=""):
    """
    Prepare the formatted message
    """
    avl_ctr = 0
    message = "*" + header + "*\n" + "=" * len(header)
    for r in logs:
        status_msg = ""
        for l in r["logs"]:
            if (
                int(l["general"])
                + int(l["hdu"])
                + int(l["icu"])
                + int(l["icuwithventilator"])
            ) <= 0:
                continue
            status_msg = (
                status_msg
                + "```\n"
                + f"Last updated: {l['timestamp']} \n"
                + f"GEN: {l['general']} | "
                + f"HDU: {l['hdu']} | "
                + f"ICU: {l['icu']} | "
                + f"V-ICU: {l['icuwithventilator']}"
                + "\n```"
            )
        if status_msg != "":
            avl_ctr = avl_ctr + 1
            if r["logs"][0]["phonenumber"] != "":
                phn_num = f"+91{r['logs'][0]['phonenumber']}"
            else:
                phn_num = ""
            message = (
                message
                + "\n*"
                + r["hospital"]
                + "*\n"
                + "📞 "
                + phn_num
                + "\n"
                + status_msg
                + "\n"
            )

    if avl_ctr == 0:
        message = message + f"\nNo beds available in {len(logs)} tracked hospital(s)"
    return message


def prepare_scheduled_message():
    """
    Prepare the message to be sent to the channel
    """

    status = read_status_logs()
    grp = status.groupby("hospital")
    logs = []
    for hosp, s in grp:
        logs.append({"hospital": hosp, "logs": get_latest(s, n_latest=1)})
    time_now = datetime.now(IST).strftime("%Y-%m-%d  %H:%M")
    header = f"*Status @ : {time_now}* \n"
    message = prepare_message(logs, header)
    _footer = "\nBot Link : @citagbedinfoline\_bot\n"
    message = message + _footer

    return message


def send_to_channel(bot):
    """
    Send the scheduled message to channel
    """
    message = prepare_scheduled_message()
    send_message(
        bot=bot,
        chat_id=SCHEDULE_CHANNEL,
        text=message,
        parse_mode=telegram.ParseMode.MARKDOWN,
    )


def process_pincode(pincode, n_latest=1):
    """
    Return the data of all hospitals in a pincode
    Format the response string
    """

    status = read_status_logs()

    sel_status, hosp_count = hosps_in_pincode(status, pincode)

    grp = sel_status.groupby("hospital")
    logs = []
    for hosp, s in grp:
        logs.append({"hospital": hosp, "logs": get_latest(s, n_latest=1)})

    if len(logs) == 0:
        message = "No hospitals found"
    else:
        message = prepare_message(logs, header=pincode)
    return message


def process_zone(zone, n_latest=1):
    """
    Return the data of all hospitals in a zone
    Format the response string
    """
    status = read_status_logs()

    sel_status, hosp_count = hosps_in_zone(status, zone)

    grp = sel_status.groupby("hospital")
    logs = []
    for hosp, s in grp:
        logs.append({"hospital": hosp, "logs": get_latest(s, n_latest=1)})

    if len(logs) == 0:
        message = "No hospitals found"
    else:
        message = prepare_message(logs, header=zone)
    return message


def process_bedtype(bedtype):
    """
    Return the data of all hospitals
    that have an available bed in the provided bedtype
    """
    bedtype_map = {
        "General": "general",
        "HDU": "hdu",
        "ICU": "icu",
        "Ventilator-ICU": "icuwithventilator",
    }

    bedtype_mapped = bedtype_map[bedtype]

    status = read_status_logs()

    sel_status, hosp_count = hosps_in_bedtype(status, bedtype_mapped)

    grp = sel_status.groupby("hospital")
    logs = []
    for hosp, s in grp:
        logs.append({"hospital": hosp, "logs": get_latest(s, n_latest=1)})

    if len(logs) == 0:
        message = "No hospitals found"
    else:
        message = prepare_message(logs, header=bedtype)
    return message


def build_menu(buttons, n_cols, header_buttons=None, footer_buttons=None):
    """
    Build a menu
    """
    menu = [buttons[i : i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, [header_buttons])
    if footer_buttons:
        menu.append([footer_buttons])
    return menu


def send_message(bot, chat_id, text, **kwargs):
    """
    Custom send_message with BIN
    """
    msg = bot.send_message(chat_id=chat_id, text=text, **kwargs)
    msg = str(msg)
    # BIN IF BIN
    if BIN_CHANNEL:
        try:
            bot.send_message(
                chat_id=BIN_CHANNEL, text=json.dumps(ast.literal_eval(msg), indent=4)
            )
        except Exception as e:
            logging.error(f"BIN Fail : {e}")
            pass


def entry(bot, update):
    """
    Handle all actions by the bot
    """
    # BIN IF BIN
    if BIN_CHANNEL:
        try:
            msg = str(update)
            bot.send_message(
                chat_id=BIN_CHANNEL, text=json.dumps(ast.literal_eval(msg), indent=4)
            )
        except Exception as e:
            logging.error(f"BIN Fail : {e}")
            pass

    # CALLBACKS
    if update.callback_query:
        if update.callback_query.message.reply_to_message.text.startswith("/zone"):
            zone = update.callback_query.data
            try:
                message = process_zone(zone)
                logging.debug(message)
                send_message(
                    bot=bot,
                    chat_id=update.callback_query.message.chat.id,
                    text=message,
                    parse_mode=telegram.ParseMode.MARKDOWN,
                )
            except Exception as e:
                logging.error(e)
                send_message(
                    bot=bot,
                    chat_id=update.callback_query.message.chat.id,
                    text="Hospital fetch failed",
                )

            return

        if update.callback_query.message.reply_to_message.text.startswith("/pincode"):
            pincode = update.callback_query.data
            try:
                message = process_pincode(pincode)
                send_message(
                    bot=bot,
                    chat_id=update.callback_query.message.chat.id,
                    text=message,
                    parse_mode=telegram.ParseMode.MARKDOWN,
                )
            except Exception as e:
                logging.error(e)
                send_message(
                    bot=bot,
                    chat_id=update.callback_query.message.chat.id,
                    text="Hospital fetch failed",
                )
                logging.error(e)

            return

        if update.callback_query.message.reply_to_message.text.startswith("/bedtype"):
            bedtype = update.callback_query.data
            try:
                message = process_bedtype(bedtype)
                send_message(
                    bot=bot,
                    chat_id=update.callback_query.message.chat.id,
                    text=message,
                    parse_mode=telegram.ParseMode.MARKDOWN,
                )
            except Exception as e:
                logging.error(e)
                send_message(
                    bot=bot,
                    chat_id=update.callback_query.message.chat.id,
                    text="Hospital fetch failed",
                )
                logging.error(e)

            return

    if update.message:

        # Load the zones and pincodes
        with open("metadata.json", "r") as f:
            meta = json.load(f)
        zones = meta["zones"]
        pincodes = meta["pincodes"]
        # ZONE
        try:
            if update.message.text.startswith("/zone"):
                bot.send_chat_action(
                    chat_id=update.message.chat.id, action=telegram.ChatAction.TYPING
                )
                button_list = []
                for zone in zones:
                    button_list.append(InlineKeyboardButton(zone, callback_data=zone))
                reply_markup = InlineKeyboardMarkup(build_menu(button_list, n_cols=2))
                send_message(
                    bot=bot,
                    chat_id=update.message.chat.id,
                    text="Which zone's hospitals do you want to check?",
                    reply_to_message_id=update.message.message_id,
                    reply_markup=reply_markup,
                )
                return
        except Exception as e:
            logging.error(e)
            send_message(
                bot=bot, chat_id=update.message.chat.id, text="Something wrong.. :/"
            )
            return

        # PINCODES
        try:
            if update.message.text.startswith("/pincode"):
                bot.send_chat_action(
                    chat_id=update.message.chat.id, action=telegram.ChatAction.TYPING
                )
                button_list = []
                for pincode in pincodes:
                    button_list.append(
                        InlineKeyboardButton(pincode, callback_data=pincode)
                    )
                reply_markup = InlineKeyboardMarkup(build_menu(button_list, n_cols=4))
                send_message(
                    bot=bot,
                    chat_id=update.message.chat.id,
                    text="Which pincode's hospitals do you want to check?",
                    reply_to_message_id=update.message.message_id,
                    reply_markup=reply_markup,
                )
                return
        except Exception as e:
            logging.error(e)
            send_message(
                bot=bot, chat_id=update.message.chat.id, text="Something wrong.. :/"
            )
            return

        # BEDTYPE
        try:
            if update.message.text.startswith("/bedtype"):
                bot.send_chat_action(
                    chat_id=update.message.chat.id, action=telegram.ChatAction.TYPING
                )
                button_list = []
                bedtypes = ["General", "HDU", "ICU", "Ventilator-ICU"]
                for bedtype in bedtypes:
                    button_list.append(
                        InlineKeyboardButton(bedtype, callback_data=bedtype)
                    )
                reply_markup = InlineKeyboardMarkup(build_menu(button_list, n_cols=1))
                send_message(
                    bot=bot,
                    chat_id=update.message.chat.id,
                    text="Which bed-type do you want to check?",
                    reply_to_message_id=update.message.message_id,
                    reply_markup=reply_markup,
                )
                return
        except Exception as e:
            logging.error(e)
            send_message(
                bot=bot, chat_id=update.message.chat.id, text="Something wrong.. :/"
            )
            return

        # TODO : FUZZY MATCH ON HOSPITAL NAMES

        # TODO : NEARBY PINCODES

        # TEST
        if update.message.text.startswith("/test"):
            update.message.reply_text("200 OK!", parse_mode=telegram.ParseMode.MARKDOWN)
            return

        # START
        if update.message.text.startswith("/help") or update.message.text.startswith(
            "/start"
        ):
            help_text = f"""
            \n*Zone*
            - Send the keyword /zone
            - Pick a zone
            - Details of hospitals in that zone is listed
            \n*Pincode*
            - Send the keyword /pincode
            - Choose a pincode
            - Latest available status of hospitals in that pincode is shown
            \n*BedType*
            - Send the keyword /bedtype
            - Choose a bedtype
            - Hospitals with the beds of bedtype chosen available is displayed
            \n\n_Send `/test` for checking if the bot is online_"""

            update.message.reply_text(
                str(help_text), parse_mode=telegram.ParseMode.MARKDOWN
            )
            return


def main():
    """
    Run the bot in perpetuity
    """

    start_time = int(time())

    try:
        BOT_TOKEN = os.environ["BOT_TOKEN"]
    except KeyError:
        logging.error("Bot credentials not found in environment")

    bot = telegram.Bot(BOT_TOKEN)

    # Do a data refresh every time bot restarts
    read_status_logs()
    update_id = 0

    # Try creating and analytics object
    try:
        lytics = Analytics()
    except Exception as e:
        logging.error(f"Analytics engine couldn't start : {e}")
        lytics = None

    sent_today = False
    while True:
        # Send scheduled message
        # with open("metadata.json", "r") as f:
        #     meta = json.load(f)
        # try:
        #     scheduled_sent_time = datetime.strptime(
        #         meta["scheduled_sent_time"], "%Y-%m-%d %H:%M:%S%z"
        #     )
        #     logging.debug(f"Last scheduled sent : {meta['scheduled_sent_time']}")
        # except KeyError:
        #     scheduled_sent_time = datetime.strptime(
        #         "1900-01-01 00:00:00+05:30", "%Y-%m-%d %H:%M:%S%z"
        #     )

        # time_now = datetime.now(IST)
        # if (time_now - scheduled_sent_time) > timedelta(minutes=SCHEDULE_MSG_MIN):
        #     send_to_channel(bot)
        #     logging.info("Sent scheduled message to channel")
        #     meta["scheduled_sent_time"] = time_now.strftime("%Y-%m-%d %H:%M:%S%z")
        #     with open("metadata.json", "w") as f:
        #         json.dump(meta, f, indent=4)

        # Send scheduled message everyday at 8AM
        time_now = datetime.now(IST)
        send_hour = time_now.strftime("%Y-%m-%d 08:00:00+05:30")
        if (
            time_now - datetime.strptime(send_hour, "%Y-%m-%d %H:%M:%S%z")
            < timedelta(minutes=1)
        ) & ~sent_today:
            send_to_channel(bot)
            sent_today = True
            logging.info("Sent scheduled message to channel")
            meta["scheduled_sent_time"] = time_now.strftime("%Y-%m-%d 08:00:00+05:30")
            with open("metadata.json", "w") as f:
                json.dump(meta, f, indent=4)
        try:
            for update in bot.get_updates(offset=update_id, timeout=10):
                update_id = update.update_id + 1
                logging.info(f"Update ID:{update_id}")
                entry(bot, update)
                # Try logging to Usage Log
                if lytics:
                    try:
                        timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S%z")
                        update_id = update_id
                        try:
                            tg_id = update["message"]["chat"]["id"]
                        except TypeError:
                            tg_id = ""
                        try:
                            tg_username = update["message"]["chat"]["username"]
                        except TypeError:
                            tg_username = ""
                        try:
                            tg_firstname = update["message"]["chat"]["first_name"]
                        except TypeError:
                            tg_firstname = ""
                        try:
                            tg_lastname = update["message"]["chat"]["last_name"]
                        except TypeError:
                            tg_lastname = ""
                        try:
                            text = update["message"]["text"]
                        except TypeError:
                            text = ""

                        row = [
                            [
                                timestamp,
                                update_id,
                                tg_id,
                                tg_username,
                                tg_firstname,
                                tg_lastname,
                                text,
                            ]
                        ]
                        logging.info(row)
                        lytics.append_rows(row)
                    except Exception as e:
                        logging.error(f"Analytics post failed : {e}")

        except NetworkError:
            sleep(1)
        except Unauthorized:
            logging.error("User has blocked the bot")
            update_id = update_id + 1
        if int(time()) - start_time > LIFESPAN:
            logging.info("Enough for the day! Passing on to next Meeseek")
            with open("/tmp/update_id", "w") as the_file:
                the_file.write(str(update_id))
            break

if __name__ == "__main__":
    main()

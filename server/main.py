import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status, HTTPException, Response, Body, BackgroundTasks
import gspread
from gspread.utils import a1_to_rowcol
from google.oauth2.service_account import Credentials
from typing import Optional
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import configparser
import uvicorn
import multiprocessing
import sys
from dateutil import parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

DEFAULT_CONFIG_CONTENT = """
[GOOGLE_SHEETS_API]
SCOPES = https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive.file
SERVICE_ACCOUNT_FILE = key.json
SPREADSHEET_URL = https://docs.google.com/spreadsheets/d/1EwF3InRV5pJYYbDWAYhBoqvXAAbJA74I5Zv8trj_ios/edit?gid=0
WORKSHEET_NAME = Sheet23
"""


if getattr(sys, "frozen", False):
    # If the application is run as a bundle (compiled by PyInstaller)
    # sys.executable is the path to the .exe file
    application_base_dir = os.path.dirname(sys.executable)
else:
    # If run as a normal .py scriptP
    # os.path.abspath(__file__) is the path to this script
    application_base_dir = os.path.dirname(os.path.abspath(__file__))

logger: logging.Logger = logging.getLogger(__name__)
worksheet: Optional[gspread.Worksheet] = None
config = configparser.ConfigParser()
app_config = {}


async def initialize_config():
    global app_config, config
    config_file_to_read = os.path.join(application_base_dir, "config.ini")
    app_config["CONFIG_FILE_ACTUAL_PATH"] = config_file_to_read

    config = configparser.ConfigParser()

    if not os.path.exists(config_file_to_read):
        logger.warning(f"Configuration file '{config_file_to_read}' not found. Creating a default one.")
        try:
            with open(config_file_to_read, "w", encoding="utf-8") as f_config:
                f_config.write(DEFAULT_CONFIG_CONTENT)
            logger.info(f"Default configuration file created at '{config_file_to_read}'.")
            raise FileNotFoundError(
                f"IMPORTANT: Configuration file '{config_file_to_read}' was just created with default values. "
                f"Please edit it with your specific details and then restart the application."
            )
        except IOError as e:
            logger.error(f"Could not create default configuration file at '{config_file_to_read}': {e}")
            raise

    try:
        if not config.read(config_file_to_read, encoding="utf-8"):
            raise ValueError(f"Configuration file '{config_file_to_read}' found but could not be properly read or is empty.")

        if "GOOGLE_SHEETS_API" in config:
            api_config_section = config["GOOGLE_SHEETS_API"]
            sheet_config_section = config["SHEET_CONFIG"]

            scopes_str = api_config_section.get("SCOPES")
            app_config["SCOPES"] = [scope.strip() for scope in scopes_str.split(",")] if scopes_str else []

            service_account_filename = api_config_section.get("SERVICE_ACCOUNT_FILE")
            if not service_account_filename:
                raise ValueError("SERVICE_ACCOUNT_FILE is not defined in config.ini under [GOOGLE_SHEETS_API]")
            app_config["SERVICE_ACCOUNT_FILE"] = service_account_filename
            app_config["SERVICE_ACCOUNT_FILE_PATH"] = os.path.join(application_base_dir, service_account_filename)

            app_config["SPREADSHEET_URL"] = api_config_section.get("SPREADSHEET_URL")
            if not app_config["SPREADSHEET_URL"] or app_config["SPREADSHEET_URL"] == "YOUR_SPREADSHEET_URL_HERE":
                raise ValueError("SPREADSHEET_URL is not configured in config.ini or is still default.")
            app_config["WORKSHEET_NAME"] = api_config_section.get("WORKSHEET_NAME", "Sheet1")

            app_config["NAME_COL"] = sheet_config_section.get("NAME_COL", "B")
            app_config["TOD_COL"] = sheet_config_section.get("TOD__COL", "C")
            app_config["DAYS_FOR_HQ_COL"] = sheet_config_section.get("DAYS_FOR_HQ_COL", "E")
            app_config["LAST_UPDATED_COL"] = sheet_config_section.get("LAST_UPDATED_COL", "J")
        else:
            raise ValueError("[GOOGLE_SHEETS_API] section not found in config file.")

    except FileNotFoundError as fnf_error:
        logger.error(str(fnf_error))
        raise
    except (configparser.Error, ValueError) as conf_error:
        logger.error(f"Configuration Error: {conf_error}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while processing the configuration: {e}")
        logger.debug(traceback.format_exc())
        raise


async def initialize_google_sheet():
    global worksheet

    SERVICE_ACCOUNT_FILE_PATH = app_config.get("SERVICE_ACCOUNT_FILE_PATH")
    SCOPES = app_config.get("SCOPES")
    SPREADSHEET_URL = app_config.get("SPREADSHEET_URL")
    WORKSHEET_NAME = app_config.get("WORKSHEET_NAME")

    if not SERVICE_ACCOUNT_FILE_PATH:
        logger.error("Service account file path not configured. Cannot initialize Google Sheet.")
        worksheet = None
        return

    if not os.path.exists(SERVICE_ACCOUNT_FILE_PATH):
        logger.error(
            f"Service account file '{SERVICE_ACCOUNT_FILE_PATH}' not found as specified in config.ini. Please ensure it's in the same directory as the executable and correctly named."
        )
        worksheet = None
        return
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE_PATH, scopes=SCOPES)
        client = gspread.authorize(creds)

        logger.info(f"Attempting to open spreadsheet by URL: {SPREADSHEET_URL}")
        spreadsheet = client.open_by_url(str(SPREADSHEET_URL))

        logger.info(f"Attempting to open worksheet by name: '{WORKSHEET_NAME}'")
        worksheet = spreadsheet.worksheet(str(WORKSHEET_NAME))

        logger.info(f"Successfully initialized Google Sheet. Spreadsheet: '{spreadsheet.title}', Worksheet: '{worksheet.title}'")

    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(
            f"Spreadsheet not found at URL: {SPREADSHEET_URL}. "
            f"Ensure the URL is correct and the sheet is shared with the service account: {creds.service_account_email if 'creds' in locals() else 'unknown'}"
        )
        worksheet = None
    except gspread.exceptions.WorksheetNotFound:
        logger.error(
            f"Worksheet '{WORKSHEET_NAME}' not found in the spreadsheet. "
            f"Available worksheets: {[ws.title for ws in spreadsheet.worksheets()] if 'spreadsheet' in locals() else 'unknown'}"
        )
        worksheet = None
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheet: {e}")
        worksheet = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialize_config()
    await initialize_google_sheet()
    yield


app = FastAPI(
    lifespan=lifespan,
)


@app.get("/health", tags=["Health"])
async def health_check():
    if worksheet:
        return {"status": "OK", "message": "Application is healthy and Google Sheet is initialized."}
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Sheet not initialized.",
        )


@app.get("/tod")
async def get_tod():
    try:
        if not worksheet:
            return Response(content=None, media_type="text/plain; charset=utf-8")

        name_col_index = a1_to_rowcol(f"{app_config['NAME_COL']}1")[1] - 1
        tod_col_index = a1_to_rowcol(f"{app_config['TOD_COL']}1")[1] - 1
        days_col_index = a1_to_rowcol(f"{app_config['DAYS_FOR_HQ_COL']}1")[1] - 1
        last_updated_col_index = a1_to_rowcol(f"{app_config['LAST_UPDATED_COL']}1")[1] - 1

        all_values = worksheet.get_all_values()

        data_rows = all_values[1:]
        strings: list[str] = []
        processed_names: set[str] = set()

        for row in data_rows:
            nm = row[name_col_index] if len(row) > name_col_index else None
            pst = row[tod_col_index] if len(row) > tod_col_index else None
            day = row[days_col_index] if len(row) > days_col_index else None
            last_updated = row[last_updated_col_index] if len(row) > last_updated_col_index else None

            if not nm or not pst:
                continue

            normalized_name = str(nm).lower()
            if normalized_name in processed_names:
                continue

            processed_names.add(normalized_name)

            gmt_str_output = None
            gmt = await convert_pacific_to_gmt(pst)
            if isinstance(gmt, datetime):
                gmt_str_output = gmt.strftime("%Y-%m-%d %H:%M:%S")
            try:
                day = int(day) if day else None
            except (ValueError, TypeError):
                day = None

            try:
                last_updated = int(last_updated) if last_updated else None
            except (ValueError, TypeError):
                last_updated = None

            data_for_json = {
                "created_at": last_updated,
                "day": day,
                "gmt": gmt_str_output,
                "name": nm,
            }
            json_part = json.dumps(data_for_json, separators=(",", ":"))
            string = f"{str(nm).lower()}|{json_part}"
            strings.append(string)
        plain_text_response_content = "\n".join(strings)
        return Response(content=plain_text_response_content, media_type="text/plain; charset=utf-8")
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.exception(f"An unexpected error occurred while processing /tod request\n{e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}",
        )


@app.post("/tod", status_code=status.HTTP_202_ACCEPTED)
async def set_tod(background_tasks: BackgroundTasks, tod_data: dict = Body(...)):
    logger.debug(f"Received POST data: {tod_data}")
    background_tasks.add_task(update_google_sheets, tod_data=tod_data)
    return {"bean dip"}


async def update_google_sheets(tod_data: dict):
    if worksheet is None:
        return
    mobs_col_index = a1_to_rowcol(f"{app_config['NAME_COL']}1")[1]
    mobs_in_column = worksheet.col_values(mobs_col_index)
    for data in tod_data.items():
        mob, string_info = data
        found_row_index = -1
        for i, name_in_sheet in enumerate(mobs_in_column):
            if str(name_in_sheet).lower() == str(mob).lower():
                found_row_index = i
                break
        if found_row_index != -1:
            row_number_to_update = found_row_index + 1
            tod_col_label = f"{app_config.get('TOD_COL')}{row_number_to_update}"
            info: dict = json.loads(string_info)
            gmt_time = info.get("gmt")
            day = info.get("day")
            update_time = info.get("created_at")
            if isinstance(gmt_time, str):
                pacific_time_object = await convert_gmt_to_pacific(gmt_time)
                if isinstance(pacific_time_object, datetime):
                    pacific_time_str_output = pacific_time_object.strftime("%m-%d-%Y %H:%M:%S")
                    worksheet.update_acell(tod_col_label, pacific_time_str_output)
                    if update_time:
                        worksheet.update_acell(f"{app_config.get('LAST_UPDATED_COL')}{row_number_to_update}", update_time)
                    if day:
                        logger.debug(day)
                        if int(day) != 0:
                            day = day + 1
                        worksheet.update_acell(f"{app_config.get('DAYS_FOR_HQ_COL')}{row_number_to_update}", day)


async def convert_gmt_to_pacific(gmt_time_str: str):
    try:
        naive_gmt_time = parser.parse(gmt_time_str)
        aware_gmt_time = naive_gmt_time.replace(tzinfo=timezone.utc)
        pacific_timezone = ZoneInfo("America/Los_Angeles")
        pacific_time = aware_gmt_time.astimezone(pacific_timezone)

        return pacific_time
    except ValueError:
        logger.error(f"The input GMT time string '{gmt_time_str}'")
        return None
    except ZoneInfoNotFoundError:
        logger.error(
            "The 'America/Los_Angeles' timezone was not found. "
            "Ensure your system's timezone database is up to date, "
            "or install the 'tzdata' package: pip install tzdata"
        )
        return None
    except Exception as e:
        logger.exception(f"An unexpected error occurred while converting GMT to Pacific: {e}")
        return None


async def convert_pacific_to_gmt(pacific_time_str: str):
    try:
        naive_pacific_time = parser.parse(pacific_time_str)
        pacific_timezone = ZoneInfo("America/Los_Angeles")
        aware_pacific_time = naive_pacific_time.replace(tzinfo=pacific_timezone)
        gmt_time = aware_pacific_time.astimezone(timezone.utc)

        return gmt_time
    except ValueError as e:
        print(f"Error: Could not parse the Pacific time string '{pacific_time_str}'. Details: {e}")
        return None
    except ZoneInfoNotFoundError:
        print(
            "Error: The 'America/Los_Angeles' timezone was not found. "
            "Ensure your system's timezone database is up to date, "
            "or install the 'tzdata' package: `pip install tzdata`"
        )
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None


if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
        uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, workers=1)
    except Exception as e:
        logger.exception(f"An error occurred during application startup.\n{e}", stack_info=True, exc_info=True)
        print("\nPress Enter to exit...")
        input()

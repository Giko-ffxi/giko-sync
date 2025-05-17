import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status, HTTPException, Response, Body, BackgroundTasks
import gspread
from google.oauth2.service_account import Credentials
from typing import Optional
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import time
import math
import configparser
import uvicorn
import multiprocessing

logger: logging.Logger = logging.getLogger(__name__)
worksheet: Optional[gspread.Worksheet] = None

config = configparser.ConfigParser()
config_file_path = "config.ini"
app_config = {}


async def initialize_config():
    global app_config
    try:
        if not config.read(config_file_path):
            raise FileNotFoundError(f"Configuration file '{config_file_path}' not found or is empty.")
        if "GOOGLE_SHEETS_API" in config:
            api_config_section = config["GOOGLE_SHEETS_API"]

            scopes_str = api_config_section.get("SCOPES")
            app_config["SCOPES"] = [scope.strip() for scope in scopes_str.split(",")] if scopes_str else []

            app_config["SERVICE_ACCOUNT_FILE"] = api_config_section.get("SERVICE_ACCOUNT_FILE", "key.json")
            SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
            app_config["SERVICE_ACCOUNT_FILE_PATH"] = os.path.join(SCRIPT_DIR, app_config["SERVICE_ACCOUNT_FILE"])

            app_config["SPREADSHEET_URL"] = api_config_section.get("SPREADSHEET_URL")
            app_config["WORKSHEET_NAME"] = api_config_section.get("WORKSHEET_NAME", "Sheet1")
        else:
            raise ValueError("Warning: [GOOGLE_SHEETS_API] section not found in config file.")

    except FileNotFoundError as fnf_error:
        logger.error(fnf_error)
    except configparser.NoSectionError as ns_error:
        logger.error(f"Error: Section not found in config file - {ns_error}")
    except configparser.NoOptionError as no_error:
        logger.error(f"Error: Option not found in config file - {no_error}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while reading the configuration: {e}")


async def initialize_google_sheet():
    global worksheet

    SERVICE_ACCOUNT_FILE_PATH = app_config.get("SERVICE_ACCOUNT_FILE_PATH")
    SCOPES = app_config.get("SCOPES")
    SPREADSHEET_URL = app_config.get("SPREADSHEET_URL")
    WORKSHEET_NAME = app_config.get("WORKSHEET_NAME")

    if not os.path.exists(SERVICE_ACCOUNT_FILE_PATH):
        logger.error(f"Service account file '{SERVICE_ACCOUNT_FILE_PATH}' not found. Please ensure it's in the correct location.")
        return
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE_PATH, scopes=SCOPES)
        client = gspread.authorize(creds)

        logger.info(f"Attempting to open spreadsheet by URL: {SPREADSHEET_URL}")
        spreadsheet = client.open_by_url(SPREADSHEET_URL)

        logger.info(f"Attempting to open worksheet by name: '{WORKSHEET_NAME}'")
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)

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
        strings: list[str] = []
        rows = worksheet.get_all_records()
        for row in rows:
            nm = row.get("NM")
            pst = row.get("ToD")
            day = row.get("Days for HQ")
            last_updated = row.get("Last Updated")
            if not nm or not pst:
                continue
            try:
                gmt = await convert_pacific_to_gmt(pst)
                gmt_str_output = gmt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                gmt_str_output = None
            try:
                day = int(day)
            except Exception:
                day = None
            try:
                last_updated = int(last_updated)
            except Exception:
                last_updated = None

            data_for_json = {
                "created_at": last_updated,
                "day": day,
                "gmt": gmt_str_output,
                "name": nm,
            }
            json_part = json.dumps(data_for_json, separators=(",", ":"))
            string = f"{nm.lower()}|{json_part}"
            strings.append(string)
        plain_text_response_content = "\n".join(strings)
        return Response(content=plain_text_response_content, media_type="text/plain; charset=utf-8")
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}",
        )


@app.post("/tod", status_code=status.HTTP_202_ACCEPTED)
async def set_tod(background_tasks: BackgroundTasks, tod_data: dict = Body(...)):
    background_tasks.add_task(update_google_sheets, tod_data=tod_data)
    return {"bean dip"}


async def update_google_sheets(tod_data: dict):
    mobs_in_column = worksheet.col_values(2)
    for data in tod_data.items():
        mob, string_info = data
        found_row_index = -1
        for i, name_in_sheet in enumerate(mobs_in_column):
            if name_in_sheet.lower() == str(mob).lower():
                found_row_index = i
                break
        if found_row_index != -1:
            row_number_to_update = found_row_index + 1
            column_c_cell_label = f"C{row_number_to_update}"
            info: dict = json.loads(string_info)
            gmt_time = info.get("gmt")
            day = info.get("day")
            update_time = math.floor(time.time())
            pacific_time_object = await convert_gmt_to_pacific(gmt_time)
            pacific_time_str_output = pacific_time_object.strftime("%m-%d-%Y %H:%M:%S")
            worksheet.update_acell(column_c_cell_label, pacific_time_str_output)
            worksheet.update_acell(f"E{row_number_to_update}", day)
            worksheet.update_acell(f"J{row_number_to_update}", update_time)


async def convert_gmt_to_pacific(gmt_time_str: str):
    try:
        naive_gmt_time = datetime.strptime(gmt_time_str, "%Y-%m-%d %H:%M:%S")
        aware_gmt_time = naive_gmt_time.replace(tzinfo=timezone.utc)
        pacific_timezone = ZoneInfo("America/Los_Angeles")
        pacific_time = aware_gmt_time.astimezone(pacific_timezone)

        return pacific_time
    except ValueError:
        print(f"Error: The input GMT time string '{gmt_time_str}' is not in the correct format ('YYYY-MM-DD HH:MM:SS').")
        return None
    except ZoneInfoNotFoundError:
        print(
            "Error: The 'America/Los_Angeles' timezone was not found. "
            "Ensure your system's timezone database is up to date, "
            "or install the 'tzdata' package: pip install tzdata"
        )
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None


async def convert_pacific_to_gmt(pacific_time_str: str):
    try:
        processed_time_str = pacific_time_str.replace("\u202f", " ")
        datetime_format = "%A, %B %d, %Y, %I:%M:%S %p"
        naive_pacific_time = datetime.strptime(processed_time_str, datetime_format)
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
    multiprocessing.freeze_support()
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False, workers=1)

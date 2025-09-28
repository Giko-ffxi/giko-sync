import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status, HTTPException, Response, Body
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
import asyncio
from functools import wraps
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

DEFAULT_CONFIG_CONTENT = """
[GOOGLE_SHEETS_API]
SCOPES = https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive.file
SERVICE_ACCOUNT_FILE = key.json
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1x_vrvqscB51G4bvmtE3prgmNwIXUdPvbOgSi6shWiYE/edit?gid=0#gid=0"
WORKSHEET_NAME = sync

[SHEET_CONFIG]
NAME_COL = B
TOD_COL = C
DAYS_FOR_HQ_COL = E
LAST_UPDATED_COL = J

[CACHE]
SHEET_CACHE_SECONDS = 30
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1
"""

if getattr(sys, "frozen", False):
    application_base_dir = os.path.dirname(sys.executable)
else:
    application_base_dir = os.path.dirname(os.path.abspath(__file__))

logger: logging.Logger = logging.getLogger(__name__)
worksheet: Optional[gspread.Worksheet] = None
config = configparser.ConfigParser()
app_config = {}

CACHE_FILE = "server_cache.json"
CACHE_FILE_PATH = os.path.join(application_base_dir, CACHE_FILE)
server_cache = {}

sheet_cache = {
    "data": None,
    "timestamp": 0,
    "ttl": 30  # seconds
}

update_queue: list[dict] = []
update_lock = asyncio.Lock()


def retry_on_api_error(max_retries: int = 3, delay: float = 1.0):
    """Decorator to retry Google Sheets API calls on transient errors."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
                except (gspread.exceptions.APIError, ConnectionError, TimeoutError) as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Max retries ({max_retries}) reached for {func.__name__}: {e}")
                        raise
                    
                    wait_time = delay * (2 ** attempt)
                    logger.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}. Retrying in {wait_time}s...")
                    
                    if asyncio.iscoroutinefunction(func):
                        await asyncio.sleep(wait_time)
                    else:
                        time.sleep(wait_time)
                except Exception as e:
                    logger.error(f"Non-retryable error in {func.__name__}: {e}")
                    raise
            return None
        return wrapper
    return decorator


class SheetManager:
    """Manages Google Sheets operations with caching and batch updates."""
    
    def __init__(self, worksheet: gspread.Worksheet):
        self.worksheet = worksheet
        self.cache_ttl = app_config.get('CACHE_TTL', 30)
        
    def is_cache_valid(self) -> bool:
        """Check if cached sheet data is still valid."""
        return (
            sheet_cache["data"] is not None and
            time.time() - sheet_cache["timestamp"] < self.cache_ttl
        )
    
    @retry_on_api_error()
    async def get_sheet_data(self, force_refresh: bool = False) -> list[list[str]]:
        """Get sheet data with caching."""
        if not force_refresh and self.is_cache_valid():
            logger.debug("Using cached sheet data")
            return sheet_cache["data"]
        
        try:
            logger.info("Fetching fresh data from Google Sheets")
            all_values = self.worksheet.get_all_values()
            
            sheet_cache["data"] = all_values
            sheet_cache["timestamp"] = time.time()
            
            return all_values
            
        except Exception as e:
            logger.error(f"Failed to fetch sheet data: {e}")
            if sheet_cache["data"] is not None:
                logger.warning("Returning stale cached data due to API error")
                return sheet_cache["data"]
            raise
    
    @retry_on_api_error()
    async def batch_update_cells(self, updates: list[dict]) -> bool:
        """Perform batch updates to reduce API calls."""
        if not updates:
            return True
            
        try:
            cell_updates = []
            
            for update in updates:
                cell_updates.append({
                    'range': update['range'],
                    'values': [[update['value']]]
                })
            
            if cell_updates:
                self.worksheet.batch_update(cell_updates)
                logger.info(f"Successfully batch updated {len(cell_updates)} cells")
                
                sheet_cache["data"] = None
                
            return True
            
        except Exception as e:
            logger.error(f"Batch update failed: {e}")
            return False


def load_cache():
    """Load cache from file."""
    global server_cache
    
    try:
        if not os.path.exists(CACHE_FILE_PATH):
            logger.info("Cache file not found. Starting with empty cache.")
            server_cache = {}
            return
            
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                logger.warning("Cache file is empty. Starting with empty cache.")
                server_cache = {}
                return
                
            server_cache = json.loads(content)
            logger.info(f"Successfully loaded cache with {len(server_cache)} entries")
            
    except json.JSONDecodeError as e:
        logger.error(f"Cache file contains invalid JSON: {e}. Starting with empty cache.")
        server_cache = {}
        try:
            backup_path = f"{CACHE_FILE_PATH}.corrupted.{int(time.time())}"
            os.rename(CACHE_FILE_PATH, backup_path)
            logger.info(f"Corrupted cache file backed up to {backup_path}")
        except OSError:
            pass
            
    except (IOError, OSError) as e:
        logger.error(f"Error reading cache file: {e}. Starting with empty cache.")
        server_cache = {}
    except Exception as e:
        logger.error(f"Unexpected error loading cache: {e}. Starting with empty cache.")
        server_cache = {}


def save_cache():
    """Save cache to file with atomic writes."""
    temp_file = f"{CACHE_FILE_PATH}.tmp"
    
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(server_cache, f, indent=2, ensure_ascii=False)
        
        if os.path.exists(CACHE_FILE_PATH):
            backup_path = f"{CACHE_FILE_PATH}.backup"
            os.replace(CACHE_FILE_PATH, backup_path)
            
        os.replace(temp_file, CACHE_FILE_PATH)
        logger.debug(f"Cache saved successfully with {len(server_cache)} entries")
        
    except (IOError, OSError) as e:
        logger.critical(f"Could not save cache to file: {e}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
    except Exception as e:
        logger.error(f"Unexpected error saving cache: {e}")


async def initialize_config():
    """Initialize configuration."""
    global app_config, config
    config_file_to_read = os.path.join(application_base_dir, "config.ini")    

    config = configparser.ConfigParser()

    if not os.path.exists(config_file_to_read):
        logger.warning(f"Configuration file '{config_file_to_read}' not found. Creating default.")
        try:
            with open(config_file_to_read, "w", encoding="utf-8") as f_config:
                f_config.write(DEFAULT_CONFIG_CONTENT)
            logger.info(f"Default configuration file created at '{config_file_to_read}'.")
            raise FileNotFoundError(
                f"IMPORTANT: Configuration file '{config_file_to_read}' was just created with default values. "
                f"Please edit it with your specific details and restart the application."
            )
        except IOError as e:
            logger.error(f"Could not create default configuration file: {e}")
            raise

    try:
        if not config.read(config_file_to_read, encoding="utf-8"):
            raise ValueError(f"Configuration file '{config_file_to_read}' could not be read or is empty.")

        if "GOOGLE_SHEETS_API" not in config:
            raise ValueError("[GOOGLE_SHEETS_API] section not found in config file.")
            
        api_config = config["GOOGLE_SHEETS_API"]
        sheet_config = config["SHEET_CONFIG"]
        cache_config = config["CACHE"]

        scopes_str = api_config.get("SCOPES", "")
        app_config["SCOPES"] = [scope.strip() for scope in scopes_str.split(",") if scope.strip()]

        service_account_filename = api_config.get("SERVICE_ACCOUNT_FILE", "").strip()
        if not service_account_filename:
            raise ValueError("SERVICE_ACCOUNT_FILE is not defined in config.ini")
        app_config["SERVICE_ACCOUNT_FILE"] = service_account_filename
        app_config["SERVICE_ACCOUNT_FILE_PATH"] = os.path.join(application_base_dir, service_account_filename)

        spreadsheet_url = api_config.get("SPREADSHEET_URL", "").strip().strip('"')
        if not spreadsheet_url or spreadsheet_url == "YOUR_SPREADSHEET_URL_HERE":
            raise ValueError("SPREADSHEET_URL is not configured properly in config.ini")
        app_config["SPREADSHEET_URL"] = spreadsheet_url
        app_config["WORKSHEET_NAME"] = api_config.get("WORKSHEET_NAME", "Sheet1").strip()

        app_config["NAME_COL"] = sheet_config.get("NAME_COL", "B").strip()
        app_config["TOD_COL"] = sheet_config.get("TOD_COL", "C").strip()  # Fixed typo from TOD__COL
        app_config["DAYS_FOR_HQ_COL"] = sheet_config.get("DAYS_FOR_HQ_COL", "E").strip()
        app_config["LAST_UPDATED_COL"] = sheet_config.get("LAST_UPDATED_COL", "J").strip()

        app_config["CACHE_TTL"] = int(cache_config.get("SHEET_CACHE_SECONDS", "30"))
        app_config["MAX_RETRY_ATTEMPTS"] = int(cache_config.get("MAX_RETRY_ATTEMPTS", "3"))
        app_config["RETRY_DELAY"] = float(cache_config.get("RETRY_DELAY_SECONDS", "1.0"))
        

        sheet_cache["ttl"] = app_config["CACHE_TTL"]

    except (configparser.Error, ValueError) as e:
        logger.error(f"Configuration Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error processing configuration: {e}")
        raise


async def initialize_google_sheet():
    """Initialize Google Sheet connection."""
    global worksheet

    service_account_path = app_config.get("SERVICE_ACCOUNT_FILE_PATH")
    scopes = app_config.get("SCOPES", [])
    spreadsheet_url = app_config.get("SPREADSHEET_URL", "NONE")
    worksheet_name = app_config.get("WORKSHEET_NAME", "NONE")
    spreadsheet = None

    if not service_account_path:
        logger.error("Service account file path not configured.")
        worksheet = None
        return

    if not os.path.exists(service_account_path):
        logger.error(f"Service account file '{service_account_path}' not found.")
        worksheet = None
        return

    try:
        logger.info("Initializing Google Sheets connection...")
        creds = Credentials.from_service_account_file(service_account_path, scopes=scopes)
        client = gspread.authorize(creds)

        logger.info(f"Opening spreadsheet: {spreadsheet_url}")
        spreadsheet = client.open_by_url(spreadsheet_url)

        logger.info(f"Opening worksheet: '{worksheet_name}'")
        worksheet = spreadsheet.worksheet(worksheet_name)

        logger.info(f"Successfully initialized. Spreadsheet: '{spreadsheet.title}', Worksheet: '{worksheet.title}'")

    except gspread.exceptions.SpreadsheetNotFound as e:
        logger.error(f"Spreadsheet not found: {e}. Check URL and permissions.")
        worksheet = None
    except gspread.exceptions.WorksheetNotFound:        
        available_sheets = []
        try:
            if 'spreadsheet' in locals() and spreadsheet:
                available_sheets = [ws.title for ws in spreadsheet.worksheets()]
        except Exception:
            pass
        logger.error(f"Worksheet '{worksheet_name}' not found. Available: {available_sheets}")
        worksheet = None
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheet: {e}")
        worksheet = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    try:
        await initialize_config()
        await initialize_google_sheet()
        load_cache()
        
        update_task = asyncio.create_task(process_update_queue())
        
        yield
        
        update_task.cancel()
        try:
            await update_task
        except asyncio.CancelledError:
            pass
        save_cache()
        
    except Exception as e:
        logger.error(f"Error during application lifecycle: {e}")
        raise


app = FastAPI(lifespan=lifespan)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check..."""
    health_status = {
        "status": "OK" if worksheet else "DEGRADED",
        "google_sheet_initialized": worksheet is not None,
        "cache_entries": len(server_cache),
        "sheet_cache_valid": sheet_cache["data"] is not None and time.time() - sheet_cache["timestamp"] < sheet_cache["ttl"]
    }
    
    if worksheet:
        return health_status
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Sheet not initialized",
        )


@app.get("/tod")
async def get_tod():
    """Get TOD data..."""
    try:
        if not worksheet:
            logger.warning("Worksheet not available, returning empty response")
            return Response(content="", media_type="text/plain; charset=utf-8")

        sheet_manager = SheetManager(worksheet)
        all_values = await sheet_manager.get_sheet_data()

        if not all_values or len(all_values) < 2:
            logger.warning("No data found in sheet")
            return Response(content="", media_type="text/plain; charset=utf-8")
        
        try:
            name_col_idx = a1_to_rowcol(f"{app_config['NAME_COL']}1")[1] - 1
            tod_col_idx = a1_to_rowcol(f"{app_config['TOD_COL']}1")[1] - 1
            days_col_idx = a1_to_rowcol(f"{app_config['DAYS_FOR_HQ_COL']}1")[1] - 1
            last_updated_col_idx = a1_to_rowcol(f"{app_config['LAST_UPDATED_COL']}1")[1] - 1
        except Exception as e:
            logger.error(f"Error parsing column indices: {e}")
            raise HTTPException(status_code=500, detail="Invalid column configuration")

        data_rows = all_values[1:]
        strings = []
        processed_names = set()

        for row_idx, row in enumerate(data_rows, start=2):
            try:
                name = row[name_col_idx] if len(row) > name_col_idx else ""
                pst = row[tod_col_idx] if len(row) > tod_col_idx else ""
                day = row[days_col_idx] if len(row) > days_col_idx else ""
                last_updated = row[last_updated_col_idx] if len(row) > last_updated_col_idx else ""

                if not name.strip() or not pst.strip():
                    continue

                normalized_name = name.lower().strip()
                if normalized_name in processed_names:
                    continue

                processed_names.add(normalized_name)

                gmt_str_output = None
                try:
                    gmt = await convert_pacific_to_gmt(pst)
                    if isinstance(gmt, datetime):
                        gmt_str_output = gmt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception as e:
                    logger.warning(f"Error converting time for {name}: {e}")

                day_int = None
                try:
                    day_int = int(day) if day.strip() else None
                except (ValueError, AttributeError):
                    pass

                last_updated_int = None
                try:
                    last_updated_int = int(last_updated) if last_updated.strip() else None
                except (ValueError, AttributeError):
                    pass

                data_for_json = {
                    "created_at": last_updated_int,
                    "day": day_int,
                    "gmt": gmt_str_output,
                    "name": name,
                }
                
                json_part = json.dumps(data_for_json, separators=(",", ":"))
                string = f"{normalized_name}|{json_part}"
                strings.append(string)

            except Exception as e:
                logger.warning(f"Error processing row {row_idx}: {e}")
                continue

        response_content = "\n".join(strings)
        return Response(content=response_content, media_type="text/plain; charset=utf-8")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in /tod endpoint: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error occurred",
        )


@app.post("/tod", status_code=status.HTTP_202_ACCEPTED)
async def set_tod(tod_data: dict = Body(...)):
    """Queue TOD updates for batch processing."""
    logger.debug(f"Received POST data with {len(tod_data)} entries")
    
    if not tod_data:
        return {"status": "error", "message": "No data provided"}
    
    async with update_lock:
        for mob, string_info in tod_data.items():
            update_queue.append({
                "mob": mob,
                "data": string_info,
                "timestamp": time.time()
            })
    
    return {"status": "accepted", "queued_updates": len(tod_data)}


async def process_update_queue():
    """Background task to process queued updates in batches."""
    while True:
        try:
            await asyncio.sleep(2)  # process queue every 2 seconds
            
            async with update_lock:
                if not update_queue:
                    continue
                    
                batch = update_queue[:15]
                update_queue[:15] = []
            
            if batch:
                await process_update_batch(batch)
                
        except asyncio.CancelledError:
            logger.info("Update queue processor cancelled")
            break
        except Exception as e:
            logger.error(f"Error in update queue processor: {e}")
            await asyncio.sleep(5)


async def process_update_batch(batch: list[dict]):
    """Process a batch of updates."""
    if not worksheet:
        logger.warning("Worksheet not available, skipping updates")
        return

    try:
        sheet_manager = SheetManager(worksheet)
        all_values = await sheet_manager.get_sheet_data(force_refresh=True)
        
        if not all_values:
            logger.error("Could not fetch sheet data for updates")
            return

        name_col_idx = a1_to_rowcol(f"{app_config['NAME_COL']}1")[1]
        name_values = [row[name_col_idx - 1] if len(row) > name_col_idx - 1 else "" 
                      for row in all_values[1:]]
        
        name_to_row = {name.lower().strip(): idx + 2 
                      for idx, name in enumerate(name_values) if name.strip()}

        updates = []
        
        for update_item in batch:
            try:
                mob = update_item["mob"]
                string_info = update_item["data"]
                
                mob_name_lower = str(mob).lower().strip()
                info = json.loads(string_info)
                
                day = info.get("day")
                gmt_time = info.get("gmt")
                update_time = info.get("created_at")

                row_number = name_to_row.get(mob_name_lower)
                if not row_number:
                    logger.warning(f"Mob '{mob}' not found in sheet")
                    continue

                if isinstance(gmt_time, str):
                    try:
                        pacific_time = await convert_gmt_to_pacific(gmt_time)
                        if isinstance(pacific_time, datetime):
                            pacific_str = pacific_time.strftime("%m-%d-%Y %H:%M:%S")
                            updates.append({
                                'range': f"{app_config['TOD_COL']}{row_number}",
                                'value': pacific_str
                            })
                    except Exception as e:
                        logger.error(f"Error converting time for {mob}: {e}")

                if update_time:
                    updates.append({
                        'range': f"{app_config['LAST_UPDATED_COL']}{row_number}",
                        'value': str(update_time)
                    })

                if day is not None:
                    try:
                        is_new_day = (mob_name_lower not in server_cache or 
                                    server_cache.get(mob_name_lower, {}).get("day") != day)

                        if is_new_day:
                            day_to_write = int(day) + 1
                            updates.append({
                                'range': f"{app_config['DAYS_FOR_HQ_COL']}{row_number}",
                                'value': str(day_to_write)
                            })

                            if mob_name_lower not in server_cache:
                                server_cache[mob_name_lower] = {}
                            server_cache[mob_name_lower]["day"] = day
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error processing day value for {mob}: {e}")

            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error for mob '{update_item['mob']}': {e}")
            except Exception as e:
                logger.error(f"Error processing update for mob '{update_item['mob']}': {e}")

        if updates:
            success = await sheet_manager.batch_update_cells(updates)
            if success:
                logger.info(f"Successfully processed batch of {len(updates)} updates")
                save_cache()
            else:
                logger.error("Batch update failed")

    except Exception as e:
        logger.error(f"Error processing update batch: {e}")


async def convert_gmt_to_pacific(gmt_time_str: str) -> Optional[datetime]:
    """Convert GMT time string to Pacific time."""
    try:
        naive_gmt_time = parser.parse(gmt_time_str)
        aware_gmt_time = naive_gmt_time.replace(tzinfo=timezone.utc)
        pacific_timezone = ZoneInfo("America/Los_Angeles")
        pacific_time = aware_gmt_time.astimezone(pacific_timezone)
        return pacific_time
        
    except (ValueError, parser.ParserError) as e:
        logger.error(f"Could not parse GMT time string '{gmt_time_str}': {e}")
        return None
    except ZoneInfoNotFoundError:
        logger.error("Pacific timezone not found. Install tzdata: pip install tzdata")
        return None
    except Exception as e:
        logger.error(f"Unexpected error converting GMT to Pacific: {e}")
        return None


async def convert_pacific_to_gmt(pacific_time_str: str) -> Optional[datetime]:
    """Convert Pacific time string to GMT."""
    try:
        naive_pacific_time = parser.parse(pacific_time_str)
        pacific_timezone = ZoneInfo("America/Los_Angeles")
        aware_pacific_time = naive_pacific_time.replace(tzinfo=pacific_timezone)
        gmt_time = aware_pacific_time.astimezone(timezone.utc)
        return gmt_time
        
    except (ValueError, parser.ParserError) as e:
        logger.error(f"Could not parse Pacific time string '{pacific_time_str}': {e}")
        return None
    except ZoneInfoNotFoundError:
        logger.error("Pacific timezone not found. Install tzdata: pip install tzdata")
        return None
    except Exception as e:
        logger.error(f"Unexpected error converting Pacific to GMT: {e}")
        return None


if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
        uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, workers=1)
    except Exception as e:
        logger.exception(f"Error during application startup: {e}")
        print("\nPress Enter to exit...")
        input()
import psycopg2
import requests
from bs4 import BeautifulSoup
import urllib3
import re
import json
import time
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import DB_URI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# SETTINGS
# ============================================================

RUN_CAUSELIST_FETCH = True
RUN_CASE_DETAILS_UPDATE = False

START_DATE = "01-06-2026"
END_DATE = "25-12-2026"

MAX_CONSECUTIVE_EMPTY_MONTHS = 3
REQUEST_DELAY_SECONDS = 0.4

DETAIL_BATCH_LIMIT = 100

SKIP_COMPLETED_DATES = True
FORCE_REFETCH_DATES = False

DISTRICTS_FILE = "districts.json"

# Pakistan time guard
PK_TZ = ZoneInfo("Asia/Karachi")
BOT_STARTED_AT = datetime.now(PK_TZ)
MAX_RUNTIME_MINUTES = int(os.getenv("MAX_RUNTIME_MINUTES", "25"))

# Bot 7:30 AM se 5:00 PM Pakistan time tak run nahi karega
BLOCK_START_HOUR = 7
BLOCK_START_MINUTE = 30
BLOCK_END_HOUR = 17
BLOCK_END_MINUTE = 0

# Resume setting:
# Aap ne Attock mein court ID 966 tak data fetch kar liya tha.
# Agar ab bhi 966 ke baad se continue karna hai to True rakhein.
# Jab resume complete ho jaye to False kar dein.
RESUME_CAUSELIST_FROM_MIDDLE = True
RESUME_DISTRICT_ID = "1"
RESUME_AFTER_COURT_ID = "966"


# ============================================================
# LOGGING
# ============================================================

def setup_logger():
    os.makedirs("logs", exist_ok=True)

    log_file = os.path.join(
        "logs",
        f"master_bot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    )

    logger = logging.getLogger("master_bot")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info(f"Log file created: {log_file}")
    return logger


logger = setup_logger()


# ============================================================
# TIME GUARD
# ============================================================

def is_blocked_time():
    now = datetime.now(PK_TZ)
    current_minutes = now.hour * 60 + now.minute

    block_start = BLOCK_START_HOUR * 60 + BLOCK_START_MINUTE
    block_end = BLOCK_END_HOUR * 60 + BLOCK_END_MINUTE

    return block_start <= current_minutes < block_end


def should_stop_runtime():
    now = datetime.now(PK_TZ)
    elapsed_minutes = (now - BOT_STARTED_AT).total_seconds() / 60

    if elapsed_minutes >= MAX_RUNTIME_MINUTES:
        logger.info(f"Runtime limit reached: {elapsed_minutes:.1f} minutes. Safe stopping.")
        return True

    if is_blocked_time():
        logger.info("Blocked time reached: 7:30 AM to 5:00 PM Pakistan time. Safe stopping.")
        return True

    return False


# ============================================================
# DISTRICTS
# ============================================================

def load_districts():
    if not os.path.exists(DISTRICTS_FILE):
        raise FileNotFoundError(f"{DISTRICTS_FILE} file nahi mili.")

    with open(DISTRICTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    districts = []

    for item in data:
        district_id = str(item.get("district_id", "")).strip()
        district_name = str(item.get("district_name", "")).strip()
        base_url = str(item.get("base_url", "")).strip().rstrip("/")

        if district_id and district_name and base_url:
            districts.append((district_id, district_name, base_url))

    return districts


# ============================================================
# HEADERS
# ============================================================

def api_headers(base_url):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{base_url}/"
    }


def page_headers(base_url):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{base_url}/"
    }


# ============================================================
# DATABASE SETUP
# ============================================================

def create_progress_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_causelist_progress (
            district_id TEXT NOT NULL,
            court_id TEXT NOT NULL,
            date_str TEXT NOT NULL,
            district_name TEXT,
            judge_name TEXT,
            status TEXT NOT NULL,
            new_saved INTEGER DEFAULT 0,
            old_found INTEGER DEFAULT 0,
            error_message TEXT,
            processed_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (district_id, court_id, date_str)
        );
    """)


def get_progress(cursor, district_id, court_id, date_str):
    cursor.execute("""
        SELECT status, new_saved, old_found
        FROM bot_causelist_progress
        WHERE district_id = %s
        AND court_id = %s
        AND date_str = %s;
    """, (district_id, court_id, date_str))

    return cursor.fetchone()


def mark_progress(
    cursor,
    district_id,
    court_id,
    date_str,
    district_name,
    judge_name,
    status,
    new_saved=0,
    old_found=0,
    error_message=""
):
    cursor.execute("""
        INSERT INTO bot_causelist_progress
        (
            district_id,
            court_id,
            date_str,
            district_name,
            judge_name,
            status,
            new_saved,
            old_found,
            error_message,
            processed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (district_id, court_id, date_str)
        DO UPDATE SET
            district_name = EXCLUDED.district_name,
            judge_name = EXCLUDED.judge_name,
            status = EXCLUDED.status,
            new_saved = EXCLUDED.new_saved,
            old_found = EXCLUDED.old_found,
            error_message = EXCLUDED.error_message,
            processed_at = NOW();
    """, (
        district_id,
        court_id,
        date_str,
        district_name,
        judge_name,
        status,
        new_saved,
        old_found,
        error_message
    ))


# ============================================================
# DATE FUNCTIONS
# ============================================================

def get_date_range(start_date, end_date):
    dates = []
    current = datetime.strptime(start_date, "%d-%m-%Y")
    end = datetime.strptime(end_date, "%d-%m-%Y")

    while current <= end:
        if current.weekday() != 6:
            dates.append(current.strftime("%d-%m-%Y"))

        current += timedelta(days=1)

    return dates


def get_month_key(date_str):
    d = datetime.strptime(date_str, "%d-%m-%Y")
    return d.strftime("%Y-%m")


# ============================================================
# JUDGES FETCH
# ============================================================

def get_judges_for_district(session, district_id, base_url):
    url = f"{base_url}/getjudges/{district_id}/0"

    try:
        response = session.get(
            url,
            headers=api_headers(base_url),
            verify=False,
            timeout=25
        )

        judges_list = []
        seen = set()

        try:
            data = response.json()

            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue

                    court_id = (
                        item.get("court_id")
                        or item.get("id")
                        or item.get("value")
                        or ""
                    )

                    judge_name = (
                        item.get("j_name_eng")
                        or item.get("name")
                        or item.get("text")
                        or f"Judge ID {court_id}"
                    )

                    court_id = str(court_id).strip()

                    if court_id.isdigit() and court_id != "0" and court_id not in seen:
                        judges_list.append((court_id, judge_name.strip()))
                        seen.add(court_id)

            elif isinstance(data, dict):
                for key, value in data.items():
                    court_id = str(key).strip()

                    if court_id.isdigit() and court_id != "0" and court_id not in seen:
                        judges_list.append((court_id, str(value).strip()))
                        seen.add(court_id)

        except Exception:
            pass

        if not judges_list:
            soup = BeautifulSoup(response.text, "html.parser")
            for opt in soup.find_all("option"):
                val = opt.get("value")
                name = opt.text.strip()

                if val and str(val).isdigit() and val != "0" and val not in seen:
                    judges_list.append((str(val), name or f"Judge ID {val}"))
                    seen.add(str(val))

        if not judges_list:
            matches = re.findall(r'"court_id"\s*:\s*(\d+)', response.text)

            for m in matches:
                if m != "0" and m not in seen:
                    judges_list.append((str(m), f"Judge ID {m}"))
                    seen.add(str(m))

        if not judges_list:
            logger.warning(f"Judges not found for district {district_id}. Response: {response.text[:300]}")

        return judges_list

    except Exception as e:
        logger.error(f"Judges API failed for district {district_id}: {e}")
        return []


# ============================================================
# RESUME HELPER
# ============================================================

def should_skip_judge_due_to_resume(district_id, court_id, resume_state):
    if not resume_state["enabled"]:
        return False

    if resume_state["unlocked"]:
        return False

    if district_id != RESUME_DISTRICT_ID:
        return True

    if str(court_id) == str(RESUME_AFTER_COURT_ID):
        resume_state["target_found"] = True
        resume_state["unlocked"] = True
        logger.info(f"RESUME point reached. Skipping court ID {court_id}. Next judge will start.")
        return True

    return True


# ============================================================
# CAUSELIST PARSER
# ============================================================

def parse_and_save_causelist(cursor, rows, district_name, judge_name, base_url):
    current_category = ""
    new_saved = 0
    old_found = 0

    for row in rows:
        cols = row.find_all(["th", "td"])

        if len(cols) == 1 and "فہرست" not in cols[0].text:
            current_category = cols[0].text.strip()

        elif len(cols) >= 5:
            case_no = None

            for col in reversed(cols):
                text_val = col.text.strip()

                if text_val.isdigit() and 3 <= len(text_val) <= 15:
                    case_no = text_val
                    break

            case_link = ""
            link_tag = row.find("a", href=True)

            if link_tag:
                case_link = link_tag["href"].strip()

                if not case_link.startswith("http"):
                    case_link = base_url + "/" + case_link.lstrip("/")

            title_ur = ""
            plaintiff_ur = ""
            defendant_ur = ""

            title_en = ""
            plaintiff_en = ""
            defendant_en = ""

            for col in cols:
                text_val = col.text.strip().replace("\n", " ")
                text_val = " ".join(text_val.split())

                if "بنام" in text_val:
                    title_ur = text_val
                    parts = text_val.split("بنام")

                    plaintiff_ur = parts[0].strip()

                    if len(parts) > 1:
                        defendant_ur = parts[1].strip()

                    break

                elif re.search(r"(?i)\bvs\.?\b", text_val):
                    title_en = text_val
                    parts = re.split(r"(?i)\bvs\.?\b", text_val)

                    plaintiff_en = parts[0].strip()

                    if len(parts) > 1:
                        defendant_en = parts[1].strip()

                    break

            if case_no and case_link:
                insert_query = """
                    INSERT INTO court_cases
                    (
                        case_no,
                        district_name,
                        judge_name,
                        category,
                        case_title_ur,
                        plaintiff_ur,
                        defendant_ur,
                        case_title_en,
                        plaintiff_en,
                        defendant_en,
                        case_link
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (case_no) DO NOTHING;
                """

                cursor.execute(
                    insert_query,
                    (
                        case_no,
                        district_name,
                        judge_name,
                        current_category,
                        title_ur,
                        plaintiff_ur,
                        defendant_ur,
                        title_en,
                        plaintiff_en,
                        defendant_en,
                        case_link
                    )
                )

                if cursor.rowcount > 0:
                    new_saved += 1
                else:
                    old_found += 1

    return new_saved, old_found


# ============================================================
# CAUSELIST FETCHER
# ============================================================

def fetch_all_causelists(conn, cursor):
    logger.info("Phase 1 started: Cause lists fetching")

    districts = load_districts()
    dates_list = get_date_range(START_DATE, END_DATE)

    logger.info(f"Districts loaded: {len(districts)}")
    logger.info(f"Date range: {START_DATE} to {END_DATE}")
    logger.info(f"Total dates excluding Sundays: {len(dates_list)}")

    if RESUME_CAUSELIST_FROM_MIDDLE:
        logger.info(
            f"RESUME enabled: district {RESUME_DISTRICT_ID}, after court ID {RESUME_AFTER_COURT_ID}"
        )

    grand_new = 0
    grand_old = 0
    grand_skipped_dates = 0
    grand_errors = 0
    grand_skipped_judges_by_resume = 0

    session = requests.Session()

    resume_state = {
        "enabled": RESUME_CAUSELIST_FROM_MIDDLE,
        "unlocked": False,
        "target_found": False
    }

    for district_id, district_name, base_url in districts:
        if should_stop_runtime():
            logger.info("Safe stop before next district.")
            return

        logger.info("==================================================")
        logger.info(f"DISTRICT: {district_name} | ID: {district_id} | URL: {base_url}")
        logger.info("==================================================")

        try:
            session.get(
                f"{base_url}/",
                headers=page_headers(base_url),
                verify=False,
                timeout=25
            )
        except Exception as e:
            logger.warning(f"Homepage session request failed for {district_name}: {e}")

        judges_list = get_judges_for_district(session, district_id, base_url)

        if not judges_list:
            logger.warning(f"No judges found for {district_name}. Skipping district.")
            continue

        logger.info(f"Total judges found in {district_name}: {len(judges_list)}")

        for court_id, judge_name in judges_list:
            if should_stop_runtime():
                logger.info("Safe stop before next judge.")
                return

            if should_skip_judge_due_to_resume(district_id, court_id, resume_state):
                grand_skipped_judges_by_resume += 1
                logger.info(
                    f"RESUME SKIP judge: {judge_name} | Court ID: {court_id} | District: {district_name}"
                )
                continue

            logger.info("-----------------------------------------------")
            logger.info(f"Judge: {judge_name} | Court ID: {court_id}")
            logger.info("-----------------------------------------------")

            judge_new = 0
            judge_old = 0
            judge_skipped = 0
            judge_errors = 0

            current_month = None
            month_total_records = 0
            empty_months = 0
            stop_judge = False

            for date_str in dates_list:
                if should_stop_runtime():
                    logger.info("Safe stop during date loop.")
                    return

                month_key = get_month_key(date_str)

                if current_month is None:
                    current_month = month_key

                if month_key != current_month:
                    if month_total_records == 0:
                        empty_months += 1
                        logger.info(
                            f"Month {current_month} blank for judge {judge_name}. Empty months: {empty_months}"
                        )
                    else:
                        empty_months = 0
                        logger.info(
                            f"Month {current_month} active for judge {judge_name}. Records: {month_total_records}"
                        )

                    if empty_months >= MAX_CONSECUTIVE_EMPTY_MONTHS:
                        logger.info(
                            f"Stopping judge because {MAX_CONSECUTIVE_EMPTY_MONTHS} consecutive blank months found: {judge_name}"
                        )
                        stop_judge = True
                        break

                    current_month = month_key
                    month_total_records = 0

                if SKIP_COMPLETED_DATES and not FORCE_REFETCH_DATES:
                    progress = get_progress(cursor, district_id, court_id, date_str)

                    if progress:
                        status, saved_count, old_count = progress

                        if status == "completed":
                            total_records = int(saved_count or 0) + int(old_count or 0)
                            month_total_records += total_records
                            judge_skipped += 1
                            grand_skipped_dates += 1

                            logger.info(
                                f"SKIP completed {date_str} | New: {saved_count} | Old: {old_count}"
                            )
                            continue

                time.sleep(REQUEST_DELAY_SECONDS)

                try:
                    url = f"{base_url}/causelist"

                    params = {
                        "clist_cid": court_id,
                        "clist_dated": date_str,
                        "district": district_id
                    }

                    response = session.get(
                        url,
                        params=params,
                        headers=page_headers(base_url),
                        verify=False,
                        timeout=30
                    )

                    soup = BeautifulSoup(response.text, "html.parser")
                    tables = soup.find_all("table")

                    if not tables:
                        mark_progress(
                            cursor,
                            district_id,
                            court_id,
                            date_str,
                            district_name,
                            judge_name,
                            "completed",
                            0,
                            0,
                            ""
                        )
                        conn.commit()
                        logger.info(f"{date_str} | No table / no record")
                        continue

                    rows = tables[0].find_all("tr")

                    new_saved, old_found = parse_and_save_causelist(
                        cursor,
                        rows,
                        district_name,
                        judge_name,
                        base_url
                    )

                    mark_progress(
                        cursor,
                        district_id,
                        court_id,
                        date_str,
                        district_name,
                        judge_name,
                        "completed",
                        new_saved,
                        old_found,
                        ""
                    )

                    conn.commit()

                    total_records = new_saved + old_found
                    month_total_records += total_records

                    judge_new += new_saved
                    judge_old += old_found

                    grand_new += new_saved
                    grand_old += old_found

                    logger.info(f"{date_str} | New: {new_saved} | Old: {old_found}")

                except Exception as e:
                    conn.rollback()
                    error_text = str(e)[:500]

                    try:
                        mark_progress(
                            cursor,
                            district_id,
                            court_id,
                            date_str,
                            district_name,
                            judge_name,
                            "error",
                            0,
                            0,
                            error_text
                        )
                        conn.commit()
                    except Exception:
                        conn.rollback()

                    judge_errors += 1
                    grand_errors += 1

                    logger.error(f"Error on {district_name} | {judge_name} | {date_str}: {e}")

            if not stop_judge and current_month:
                if month_total_records == 0:
                    logger.info(f"Last checked month {current_month} blank for judge {judge_name}")
                else:
                    logger.info(
                        f"Last checked month {current_month} active for judge {judge_name}. Records: {month_total_records}"
                    )

            logger.info(
                f"Judge summary | New: {judge_new} | Old: {judge_old} | Skipped dates: {judge_skipped} | Errors: {judge_errors}"
            )

    logger.info("==================================================")
    logger.info("Phase 1 completed: Cause lists fetching")
    logger.info(f"Grand New Cases: {grand_new}")
    logger.info(f"Grand Old Cases: {grand_old}")
    logger.info(f"Grand Skipped Completed Dates: {grand_skipped_dates}")
    logger.info(f"Grand Skipped Judges By Resume: {grand_skipped_judges_by_resume}")
    logger.info(f"Grand Errors: {grand_errors}")
    logger.info("==================================================")


# ============================================================
# CASE DETAILS UPDATE
# ============================================================

def fetch_pending_case_details(conn, cursor):
    logger.info("Phase 2 started: Pending case details update")

    session = requests.Session()

    if DETAIL_BATCH_LIMIT is None:
        cursor.execute("""
            SELECT id, case_no, case_link
            FROM court_cases
            WHERE case_status IS NULL
            AND case_link IS NOT NULL;
        """)
    else:
        cursor.execute("""
            SELECT id, case_no, case_link
            FROM court_cases
            WHERE case_status IS NULL
            AND case_link IS NOT NULL
            LIMIT %s;
        """, (DETAIL_BATCH_LIMIT,))

    cases = cursor.fetchall()

    if not cases:
        logger.info("No pending case details found.")
        return

    logger.info(f"Pending cases selected for detail update: {len(cases)}")

    updated_count = 0
    error_count = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    for db_id, case_no, case_link in cases:
        if should_stop_runtime():
            logger.info("Safe stop during case details update.")
            return

        logger.info(f"Fetching details for case: {case_no}")

        time.sleep(1)

        try:
            response = session.get(
                case_link,
                headers=headers,
                verify=False,
                timeout=30
            )

            soup = BeautifulSoup(response.text, "html.parser")

            title_en = ""
            plaintiff_en = ""
            defendant_en = ""

            status = ""
            inst_date = ""
            fir_no = ""
            fir_year = ""
            police_station = ""
            offence = ""

            disposal_decided_by = ""
            disposal_decision_date = ""
            disposal_decision_type = ""
            disposal_short_order = ""

            proceedings = []

            title_h3 = soup.find("h3", class_="case-title")

            if title_h3:
                urdu_span = title_h3.find("span", class_="urdu-font-22")

                if urdu_span:
                    urdu_span.extract()

                raw_title_en = title_h3.text.replace("\n", " ").strip()
                title_en = " ".join(raw_title_en.split())

                if re.search(r"(?i)\bvs\.?\b", title_en):
                    parts = re.split(r"(?i)\bvs\.?\b", title_en)

                    plaintiff_en = parts[0].strip()

                    if len(parts) > 1:
                        defendant_en = parts[1].strip()
                else:
                    plaintiff_en = title_en

            for tr in soup.find_all("tr"):
                th_tags = tr.find_all("th")

                if len(th_tags) >= 4 and "Case Status" in th_tags[0].text:
                    next_tr = tr.find_next_sibling("tr")

                    if next_tr:
                        tds = next_tr.find_all("td")

                        if len(tds) >= 4:
                            status = tds[0].text.strip()
                            inst_date = tds[2].text.strip()

                if len(th_tags) >= 4 and "FIR No" in th_tags[0].text:
                    next_tr = tr.find_next_sibling("tr")

                    if next_tr:
                        tds = next_tr.find_all("td")

                        if len(tds) >= 4:
                            fir_no = tds[0].text.strip()
                            fir_year = tds[1].text.strip()
                            police_station = tds[2].text.strip()
                            offence = tds[3].text.strip()

            for h4 in soup.find_all("h4"):
                if "Disposal Detail" in h4.text:
                    disp_table = h4.find_next_sibling("table")

                    if disp_table:
                        rows = disp_table.find_all("tr")

                        if len(rows) > 1:
                            d_tds = rows[1].find_all("td")

                            if len(d_tds) >= 3:
                                disposal_decided_by = d_tds[0].text.strip()
                                disposal_decision_date = d_tds[1].text.strip()
                                disposal_decision_type = d_tds[2].text.strip()

                        if len(rows) > 2:
                            disposal_short_order = rows[2].text.replace("Short Order::", "").strip()

                    break

            for h4 in soup.find_all("h4"):
                if "Proceeding History" in h4.text:
                    proc_table = h4.find_next_sibling("table")

                    if proc_table:
                        current_judge = ""

                        for row in proc_table.find_all("tr"):
                            h5 = row.find("h5")

                            if h5:
                                current_judge = h5.text.strip()
                            else:
                                cols = row.find_all("td")

                                if len(cols) >= 4 and cols[0].text.strip().isdigit():
                                    proceedings.append({
                                        "Judge": current_judge,
                                        "Date": cols[1].text.strip(),
                                        "Stage": cols[2].text.strip(),
                                        "Order": cols[3].text.strip()
                                    })

                    break

            proceedings_json = json.dumps(proceedings, ensure_ascii=False)

            update_query = """
                UPDATE court_cases SET
                    case_status = %s,
                    institution_date = %s,
                    fir_no = %s,
                    fir_year = %s,
                    police_station = %s,
                    offence = %s,
                    case_title_en = %s,
                    plaintiff_en = %s,
                    defendant_en = %s,
                    disposal_decided_by = %s,
                    disposal_decision_date = %s,
                    disposal_decision_type = %s,
                    disposal_short_order = %s,
                    proceedings = %s,
                    last_updated = NOW()
                WHERE id = %s;
            """

            cursor.execute(
                update_query,
                (
                    status,
                    inst_date,
                    fir_no,
                    fir_year,
                    police_station,
                    offence,
                    title_en,
                    plaintiff_en,
                    defendant_en,
                    disposal_decided_by,
                    disposal_decision_date,
                    disposal_decision_type,
                    disposal_short_order,
                    proceedings_json,
                    db_id
                )
            )

            conn.commit()

            updated_count += 1
            logger.info(f"Done details: {case_no} | {title_en}")

        except Exception as e:
            conn.rollback()
            error_count += 1
            logger.error(f"Case detail error | Case: {case_no} | Error: {e}")

    logger.info("==================================================")
    logger.info("Phase 2 completed: Pending case details update")
    logger.info(f"Details updated: {updated_count}")
    logger.info(f"Detail errors: {error_count}")
    logger.info("==================================================")


# ============================================================
# MAIN
# ============================================================

def run_master_bot():
    logger.info("Master Bot started")
    logger.info(f"Pakistan time now: {datetime.now(PK_TZ).strftime('%Y-%m-%d %I:%M:%S %p')}")

    if is_blocked_time():
        logger.info("Current time blocked hai: 7:30 AM to 5:00 PM Pakistan time. Bot exit.")
        return

    conn = None
    cursor = None

    try:
        conn = psycopg2.connect(DB_URI)
        cursor = conn.cursor()

        logger.info("Database connected")

        create_progress_table(cursor)
        conn.commit()

        if RUN_CAUSELIST_FETCH:
            fetch_all_causelists(conn, cursor)
        else:
            logger.info("Phase 1 skipped: RUN_CAUSELIST_FETCH = False")

        if should_stop_runtime():
            logger.info("Safe stop before Phase 2.")
            return

        if RUN_CASE_DETAILS_UPDATE:
            fetch_pending_case_details(conn, cursor)
        else:
            logger.info("Phase 2 skipped: RUN_CASE_DETAILS_UPDATE = False")

        logger.info("MASHALLAH! Master Bot completed successfully")

    except Exception as e:
        if conn:
            conn.rollback()

        logger.error(f"Master Bot fatal error: {e}")

    finally:
        if cursor:
            cursor.close()

        if conn:
            conn.close()

        logger.info("Database connection closed")


if __name__ == "__main__":
    run_master_bot()

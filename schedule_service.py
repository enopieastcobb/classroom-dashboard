"""
Parses the weekly student-schedule Google Sheet into structured session entries.

Each day is a separate sheet tab, hand-maintained as a visual grid rather than a
normalized table: row 1 holds time-of-day headers per column; column A alternates
between a subject/program label ("Math", "English", "Weenopi") and, on a nearby
row within that same block, the teacher's name. Every other column matching a
time header can hold a student's name on any row within that block.
"""
import datetime
import logging
import re
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SUBJECTS = {"math", "english", "weenopi"}

# The right-hand columns hold one-off make-ups as their own little table:
# Make-up Date | Student Name | Make-up time | Subject.
# They sit at different column letters per tab (the grids are different widths),
# so they're located by HEADER TEXT rather than position. They never leak into
# the main grid because their row-1 headers aren't times, so time_by_col skips
# them.
MAKEUP_HEADERS = {
    'make-up date': 'date',
    'student name': 'student_name',
    'make-up time': 'time',
    'subject': 'subject',
}


SHEETS_EPOCH = datetime.date(1899, 12, 30)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _coerce_date(value: Any) -> Optional[datetime.date]:
    """
    A make-up date as a real date, or None when the cell isn't one.

    Handles every form the cell arrives in: a datetime/date (openpyxl), a
    Sheets serial number, and free text like "August" -- which is NOT a date,
    so it returns None and the caller surfaces the row as date-unspecified
    rather than dropping it.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)):
        try:
            return SHEETS_EPOCH + datetime.timedelta(days=int(value))
        except (ValueError, OverflowError):
            return None
    return None


def _normalize_teacher(raw: str) -> str:
    name = raw.strip()
    name = re.sub(r'^(mr|mrs|ms)\.?\s*', '', name, flags=re.IGNORECASE)
    return name.strip()


def _to_afternoon(hour: int) -> int:
    """
    The sheet stores time headers with no AM/PM marker (e.g. bare `time(5, 0)`).
    This tutoring center only ever runs weekday sessions in the afternoon/evening,
    so a raw hour of 1-7 always means PM, never early-morning AM.
    """
    return hour + 12 if 1 <= hour <= 7 else hour


def _format_time(value: Any) -> Optional[str]:
    """Formats a header cell into a display time string, or None if it isn't a time."""
    if isinstance(value, datetime.time):
        return datetime.time(_to_afternoon(value.hour) % 24, value.minute).strftime("%I:%M %p").lstrip("0")
    if isinstance(value, datetime.datetime):
        return datetime.time(_to_afternoon(value.time().hour) % 24, value.minute).strftime("%I:%M %p").lstrip("0")
    if isinstance(value, str):
        s = value.strip()
        # Accepts "3:00 PM", "3 PM", and the Sheets display form "3:00:00 PM".
        m = re.match(r'^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap]\.?m\.?)$', s, re.IGNORECASE)
        if m:
            hour = int(m.group(1)) % 12
            if m.group(3).lower().startswith('p'):
                hour += 12
            return datetime.time(hour, int(m.group(2) or 0)).strftime("%I:%M %p").lstrip("0")
    return None


def parse_day_grid(
    grid: List[List[Any]],
    block_start_rows: "set[int]",
) -> "tuple[List[Dict[str, Any]], List[str]]":
    """
    Parses one day-tab's raw grid (list of rows, each a list of cell values,
    as returned by the Sheets API) into a flat list of
    {time, subject, teacher, student_name} session entries.

    `block_start_rows` is the set of 0-indexed row positions (within the data
    rows, i.e. excluding the header row) that carry a top border in column A
    -- a horizontal line the spreadsheet owner draws to mark the boundary
    between one teacher's group and the next. This is real structural
    metadata from the sheet's formatting, not inferred from row position, so
    it correctly handles chains of 3+ teachers sharing the same repeated
    subject label ("Math" restated for each new teacher) where row-offset
    heuristics broke down.

    Returns (entries, warnings). `warnings` flags any student-looking cell
    that fell outside every recognized block, or any block missing a subject
    or teacher label -- these would otherwise be silently dropped/misattributed
    with no indication anything was wrong.
    """
    if not grid:
        return [], []

    header_row = grid[0]
    time_by_col: Dict[int, str] = {}
    for col_idx, value in enumerate(header_row):
        time_str = _format_time(value)
        if time_str:
            time_by_col[col_idx] = time_str

    data_rows = grid[1:]
    warnings: List[str] = []

    # Block boundaries come directly from the border lines, plus an implicit
    # boundary at the very start of the data (row 0 may or may not carry its
    # own border -- the first block still starts there either way).
    boundaries = sorted(set(block_start_rows) | {0})
    boundaries = [b for b in boundaries if b < len(data_rows)]

    entries: List[Dict[str, Any]] = []
    for bi, start_i in enumerate(boundaries):
        end_i = boundaries[bi + 1] if bi + 1 < len(boundaries) else len(data_rows)
        block_rows = data_rows[start_i:end_i]

        # Find the subject and teacher labels anywhere within this
        # border-defined block (order within the block doesn't matter now --
        # borders already tell us where the block itself starts and ends).
        subject = None
        teacher = None
        for row in block_rows:
            col_a = row[0] if len(row) > 0 else None
            if _is_blank(col_a):
                continue
            label = str(col_a).strip()
            if label.lower() in SUBJECTS:
                if subject is None:
                    subject = label.title()
            elif teacher is None:
                teacher = _normalize_teacher(label)

        if subject is None:
            for row in block_rows:
                for col_idx in time_by_col:
                    if col_idx < len(row) and not _is_blank(row[col_idx]):
                        warnings.append(
                            f"Rows {start_i + 2}-{end_i + 1}: '{row[col_idx]}' found in a block "
                            f"with no recognized subject label ({sorted(SUBJECTS)}) -- not attributed."
                        )
            continue

        if teacher is None:
            warnings.append(
                f"Block at rows {start_i + 2}-{end_i + 1} (subject '{subject}') has no teacher "
                f"name in column A -- students in this block will show teacher=None."
            )

        for row in block_rows:
            for col_idx, time_str in time_by_col.items():
                if col_idx >= len(row):
                    continue
                value = row[col_idx]
                if _is_blank(value):
                    continue
                student_name = str(value).strip()
                if not student_name:
                    continue
                entries.append({
                    "time": time_str,
                    "subject": subject,
                    "teacher": teacher,
                    "student_name": student_name,
                })

    return entries, warnings


def parse_makeups(grid: List[List[Any]]) -> "tuple[List[Dict[str, Any]], List[str]]":
    """
    Reads the make-up table from a day tab's right-hand columns.

    Returns (makeups, warnings). Each make-up is
    {date, date_raw, student_name, time, subject}, where `date` is a real date
    or None. Identical rows are de-duplicated -- the sheet currently repeats
    one -- and a row missing a student name is reported rather than dropped
    silently.
    """
    if not grid:
        return [], []

    header = grid[0]
    col_of: Dict[str, int] = {}
    for idx, value in enumerate(header):
        key = str(value).strip().lower() if value is not None else ''
        if key in MAKEUP_HEADERS:
            col_of[MAKEUP_HEADERS[key]] = idx
    if 'student_name' not in col_of:
        return [], []

    def cell(row, field):
        idx = col_of.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    makeups: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen = set()
    for r, row in enumerate(grid[1:], start=2):
        raw_date = cell(row, 'date')
        name = cell(row, 'student_name')
        raw_time = cell(row, 'time')
        subject = cell(row, 'subject')

        if all(_is_blank(v) for v in (raw_date, name, raw_time, subject)):
            continue
        if _is_blank(name):
            warnings.append(
                f"Make-up row {r} has no student name "
                f"(date={raw_date!r}, time={raw_time!r}, subject={subject!r}) -- skipped."
            )
            continue

        parsed_date = _coerce_date(raw_date)
        entry = {
            "date": parsed_date,
            "date_raw": '' if _is_blank(raw_date) else str(raw_date).strip(),
            "student_name": str(name).strip(),
            "time": _format_time(raw_time) or '',
            "subject": str(subject).strip().title() if not _is_blank(subject) else '',
        }
        key = (entry["date"], entry["date_raw"], entry["student_name"],
               entry["time"], entry["subject"])
        if key in seen:
            continue
        seen.add(key)

        if parsed_date is None and not _is_blank(raw_date):
            warnings.append(
                f"Make-up row {r} ({entry['student_name']}): date {raw_date!r} isn't a "
                f"real date, so it can't be matched to a session -- shown as undated."
            )
        makeups.append(entry)

    return makeups, warnings


class ScheduleService:
    """Reads the weekly schedule Sheet and exposes it as parsed session entries."""

    DAYS = ["Tuesday", "Wednesday", "Thursday", "Saturday"]

    # Requesting only these fields keeps the response small -- borders and
    # values only, not every formatting property Sheets tracks.
    _FIELDS = (
        "sheets.data.rowData.values("
        "formattedValue,userEnteredValue,"
        "userEnteredFormat(numberFormat.type,borders(top.style,bottom.style)),"
        "effectiveFormat(numberFormat.type,borders(top.style,bottom.style))"
        ")"
    )

    def __init__(self, spreadsheet_id: str, credentials):
        self.spreadsheet_id = spreadsheet_id
        # static_discovery avoids fetching the discovery document over the
        # network on every construction.
        try:
            self.service = build('sheets', 'v4', credentials=credentials,
                                 cache_discovery=False, static_discovery=True)
        except Exception as e:
            logger.warning(f"static discovery unavailable for sheets v4 ({e}); fetching over network.")
            self.service = build('sheets', 'v4', credentials=credentials, cache_discovery=False)

    def get_day_schedule(self, day: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        One Sheets read gives both the recurring roster and the one-off
        make-ups, returned as {"entries": [...], "makeups": [...]}.
        """
        if day not in self.DAYS:
            raise ValueError(f"Unknown day '{day}'. Expected one of {self.DAYS}.")

        # includeGridData is required for cell-level data (values, borders);
        # without it `get` returns only spreadsheet/sheet metadata.
        result = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            ranges=[f"{day}!A1:Z100"],
            includeGridData=True,
            fields=self._FIELDS,
        ).execute()

        sheets = result.get('sheets', [])
        row_data = sheets[0]['data'][0].get('rowData', []) if sheets else []

        grid: List[List[Any]] = []
        col_a_top: Dict[int, bool] = {}
        col_a_bottom: Dict[int, bool] = {}
        for row_idx, row in enumerate(row_data):
            cells = row.get('values', [])
            grid_row = [self._cell_value(cell) for cell in cells]
            grid.append(grid_row)
            if cells:
                col_a_top[row_idx] = self._has_border(cells[0], 'top')
                col_a_bottom[row_idx] = self._has_border(cells[0], 'bottom')

        # A block boundary exists between row-1 and row if EITHER row has its
        # own top border OR the row above has a bottom border -- the sheet's
        # border styling isn't always applied symmetrically on both sides.
        # Row 0 is the header; block_start_rows is 0-indexed within the data
        # rows below it, so a boundary at raw row index `ri` (ri >= 1) maps
        # to data-row index `ri - 1`.
        block_start_rows = set()
        for ri in range(1, len(row_data)):
            if col_a_top.get(ri) or col_a_bottom.get(ri - 1):
                block_start_rows.add(ri - 1)

        entries, warnings = parse_day_grid(grid, block_start_rows)
        makeups, makeup_warnings = parse_makeups(grid)
        for w in warnings + makeup_warnings:
            logger.warning(f"[{day} schedule] {w}")
        return {"entries": entries, "makeups": makeups}

    def get_day_entries(self, day: str) -> List[Dict[str, Any]]:
        """Just the recurring weekly roster, without the one-off make-ups."""
        return self.get_day_schedule(day)["entries"]

    @staticmethod
    def _has_border(cell: Dict[str, Any], side: str) -> bool:
        """
        Checks both format views: a border typed directly into the sheet shows
        up under userEnteredFormat, but one inherited from a range/theme only
        appears under effectiveFormat. Missing either would silently collapse
        every teacher block into one.
        """
        for view in ('userEnteredFormat', 'effectiveFormat'):
            if cell.get(view, {}).get('borders', {}).get(side, {}).get('style'):
                return True
        return False

    @staticmethod
    def _cell_value(cell: Dict[str, Any]) -> Any:
        uev = cell.get('userEnteredValue', {})
        if 'stringValue' in uev:
            return uev['stringValue']
        if 'numberValue' in uev:
            # Check both format views -- a format applied to a whole row or
            # column only shows up under effectiveFormat.
            kinds = {
                cell.get(view, {}).get('numberFormat', {}).get('type')
                for view in ('userEnteredFormat', 'effectiveFormat')
            }
            if 'TIME' in kinds or 'DATE_TIME' in kinds:
                total_minutes = round(uev['numberValue'] * 24 * 60) % (24 * 60)
                hours, minutes = divmod(total_minutes, 60)
                return datetime.time(hours, minutes)
            if 'DATE' in kinds:
                # Sheets serial dates count from 1899-12-30.
                return SHEETS_EPOCH + datetime.timedelta(days=int(uev['numberValue']))
            return uev['numberValue']
        # Fall back to what the sheet displays (e.g. "3:00:00 PM") so a time
        # header still parses even if the numeric/format pair is missing.
        return cell.get('formattedValue')

import base64
import json
import os
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive",
]
# Anchored next to this file so the bot finds them no matter where it's launched from
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")

GOOGLE_DOC_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


#Credentials are cached after the first load so every tool call doesn't re-read and
#re-parse token.json from disk. The lock guards loading/refreshing since tools run on
#worker threads. Service objects from build() are deliberately NOT cached - they're
#not thread-safe, and building one is cheap (no network call).
_creds: Credentials | None = None
_creds_lock = threading.Lock()

def _get_credentials() -> Credentials:
    global _creds
    with _creds_lock:
        if _creds is None:
            if not os.path.exists(TOKEN_FILE):
                raise RuntimeError(
                    'Google account not connected. Run: python -c "import google_services; '
                    'google_services.setup_auth()" once, then restart the bot.'
                )
            _creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not _creds.valid:
            if _creds.expired and _creds.refresh_token:
                _creds.refresh(Request())
                with open(TOKEN_FILE, "w") as f:
                    f.write(_creds.to_json())
            else:
                _creds = None  #drop the bad cache so a fixed token.json gets picked up next call
                raise RuntimeError("Google credentials invalid. Re-run setup_auth().")
        return _creds


def setup_auth() -> None:
    """One-time interactive setup - run manually from a terminal, NOT from the bot
    (opens a browser and blocks until you finish the consent flow)."""
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print("Google account connected successfully.")


def _gmail():
    return build("gmail", "v1", credentials=_get_credentials())


def _calendar():
    return build("calendar", "v3", credentials=_get_credentials())


def _drive():
    return build("drive", "v3", credentials=_get_credentials())


def _sheets():
    #Sheets API is authorized by the existing full 'drive' scope - no new scope/token needed
    return build("sheets", "v4", credentials=_get_credentials())


# Gmail

def gmail_list_messages(max_results: int = 10, query: str = "") -> str:
    """Lists recent Gmail messages, optionally filtered with a Gmail search query.
    Returns each message's id, sender, subject, date, and a snippet. Use the id with
    gmail_read_message to see the full message.

    Args:
        max_results: Maximum number of messages to return. Defaults to 10.
        query: Optional Gmail search query, e.g. 'is:unread' or 'from:someone@example.com'.
    """
    try:
        service = _gmail()
        results = service.users().messages().list(
            userId="me", maxResults=max_results, q=query
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return "No messages found."
        lines = []
        for msg in messages:
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            snippet = full.get("snippet", "")
            lines.append(
                f"{msg['id']} | From: {headers.get('From', '?')} | "
                f"Subject: {headers.get('Subject', '(no subject)')} | "
                f"Date: {headers.get('Date', '?')} | {snippet}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _extract_gmail_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        body = _extract_gmail_body(part)
        if body:
            return body
    return ""


def gmail_read_message(message_id: str) -> str:
    """Reads the full content (sender, subject, date, body) of a Gmail message by id.

    Args:
        message_id: The id of the message, from gmail_list_messages.
    """
    try:
        service = _gmail()
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        body = _extract_gmail_body(payload)[:2000]
        return (
            f"From: {headers.get('From', '?')}\n"
            f"Subject: {headers.get('Subject', '(no subject)')}\n"
            f"Date: {headers.get('Date', '?')}\n\n"
            f"{body}"
        )
    except Exception as e:
        return f"Error: {e}"


def gmail_send_email(to: str, subject: str, body: str) -> str:
    """Sends an email from the user's Gmail account.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.
    """
    try:
        service = _gmail()
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent to {to} with subject '{subject}'"
    except Exception as e:
        return f"Error: {e}"


def gmail_mark_as_read(message_id: str) -> str:
    """Marks a Gmail message as read so it won't show up in 'is:unread' searches again.

    Args:
        message_id: The id of the message, from gmail_list_messages.
    """
    try:
        service = _gmail()
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return f"Marked message {message_id} as read."
    except Exception as e:
        return f"Error: {e}"


# Calendar

def calendar_list_events(max_results: int = 10) -> str:
    """Lists the user's upcoming Google Calendar events, soonest first.

    Args:
        max_results: Maximum number of events to return. Defaults to 10.
    """
    try:
        service = _calendar()
        now = datetime.now(timezone.utc).isoformat()
        results = service.events().list(
            calendarId="primary", timeMin=now, maxResults=max_results,
            singleEvents=True, orderBy="startTime",
        ).execute()
        events = results.get("items", [])
        if not events:
            return "No upcoming events found."
        lines = []
        for event in events:
            start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date"))
            end = event.get("end", {}).get("dateTime", event.get("end", {}).get("date"))
            lines.append(f"{event['id']} | {event.get('summary', '(no title)')} | {start} -> {end}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def calendar_create_event(summary: str, start: str, end: str, description: str = "") -> str:
    """Creates an event on the user's primary Google Calendar. Use the current date/time
    from the system info to resolve relative dates like 'tomorrow' or 'next Monday'.

    Args:
        summary: Event title.
        start: Start time as an RFC3339 datetime with offset, e.g. 2026-06-12T15:00:00-04:00.
        end: End time as an RFC3339 datetime with offset, e.g. 2026-06-12T16:00:00-04:00.
        description: Optional event description.
    """
    try:
        service = _calendar()
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"Event created: {created.get('summary')} ({start} -> {end})\n{created.get('htmlLink')}"
    except Exception as e:
        return f"Error: {e}"


# Drive

def drive_list_files(query: str = "", max_results: int = 10) -> str:
    """Lists files in the user's Google Drive, most recently modified first, optionally
    filtered with a Drive search query.

    Args:
        query: Optional Drive search query, e.g. "name contains 'budget'".
        max_results: Maximum number of files to return. Defaults to 10.
    """
    try:
        service = _drive()
        results = service.files().list(
            q=query or None, pageSize=max_results, orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return "No files found."
        return "\n".join(
            f"{f['id']} | {f['name']} | {f['mimeType']} | {f['modifiedTime']}"
            for f in files
        )
    except Exception as e:
        return f"Error: {e}"


def drive_read_file(file_id: str) -> str:
    """Reads the text content of a file in the user's Google Drive by id (Google
    Docs/Sheets/Slides are exported as text/CSV).

    Args:
        file_id: The id of the file, from drive_list_files.
    """
    try:
        service = _drive()
        meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime_type = meta["mimeType"]
        if mime_type in GOOGLE_DOC_EXPORT_MIME:
            data = service.files().export(fileId=file_id, mimeType=GOOGLE_DOC_EXPORT_MIME[mime_type]).execute()
        else:
            data = service.files().get_media(fileId=file_id).execute()
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = str(data)
        return f"{meta['name']}:\n{text[:3000]}"
    except Exception as e:
        return f"Error: {e}"


def drive_download_file(file_id: str, destination_path: str) -> str:
    """Downloads a file from the user's Google Drive to the local filesystem. Use this to
    save a Drive file to disk (drive_read_file only shows text content in the conversation).
    Native Google Docs/Sheets/Slides are exported as text/CSV; everything else (PDFs,
    images, zips, ...) is downloaded as-is. Streams in chunks, so large files are fine.

    Args:
        file_id: The id of the file, from drive_list_files.
        destination_path: Absolute local path to save the file to, including the filename.
    """
    try:
        service = _drive()
        meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime_type = meta["mimeType"]
        if mime_type in GOOGLE_DOC_EXPORT_MIME:
            request = service.files().export_media(fileId=file_id, mimeType=GOOGLE_DOC_EXPORT_MIME[mime_type])
        else:
            request = service.files().get_media(fileId=file_id)
        parent = os.path.dirname(destination_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(destination_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        size = os.path.getsize(destination_path)
        return f"Downloaded '{meta['name']}' ({size} bytes) to {destination_path}"
    except Exception as e:
        return f"Error: {e}"


def drive_upload_file(name: str, content: str, mime_type: str = "text/plain") -> str:
    """Creates a new file with the given text content in the user's Google Drive.

    Args:
        name: Name for the new file.
        content: Text content of the file.
        mime_type: MIME type of the content. Defaults to text/plain.
    """
    try:
        service = _drive()
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
        created = service.files().create(body={"name": name}, media_body=media, fields="id,webViewLink").execute()
        return f"File created: {name}\n{created.get('webViewLink')}"
    except Exception as e:
        return f"Error: {e}"


# Sheets

SHEETS_MAX_ROWS = 100  # cap read output so a huge sheet doesn't flood the model's context


def _parse_values(values_json: str) -> list:
    """Parses a JSON 2D array (list of rows, each a list of cells) for a write. Raises
    ValueError with a clear message if the shape is wrong, so the tool returns a helpful
    error instead of a raw traceback."""
    data = json.loads(values_json)  # may raise json.JSONDecodeError (a ValueError subclass)
    if not isinstance(data, list) or not all(isinstance(row, list) for row in data):
        raise ValueError("values_json must be a JSON array of arrays, e.g. [[\"Name\",\"Age\"],[\"Ori\",22]]")
    return data


def sheets_read(spreadsheet_id: str, range: str = "") -> str:
    """Reads the contents of a Google Sheet so you can see what's in it. To find a
    spreadsheet's id, use drive_list_files (spreadsheets have mimeType
    'application/vnd.google-apps.spreadsheet') and take the id from the result.

    Args:
        spreadsheet_id: The spreadsheet's id (the long token in its URL, or from drive_list_files).
        range: An A1-notation range like 'Sheet1!A1:D20'. Leave empty to list the tab names and dump the first tab's cells.
    """
    try:
        service = _sheets()
        if not range:
            meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
            if not titles:
                return "This spreadsheet has no tabs."
            range = titles[0]  # default to the first tab
            header = f"Tabs: {', '.join(titles)}\nShowing '{titles[0]}':\n"
        else:
            header = f"{range}:\n"
        result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range).execute()
        rows = result.get("values", [])
        if not rows:
            return header + "(no data in this range)"
        shown = rows[:SHEETS_MAX_ROWS]
        body = "\n".join(" | ".join(str(cell) for cell in row) for row in shown)
        if len(rows) > SHEETS_MAX_ROWS:
            body += f"\n... ({len(rows) - SHEETS_MAX_ROWS} more rows not shown - request a narrower range to see them)"
        return header + body
    except Exception as e:
        return f"Error: {e}"


def sheets_update(spreadsheet_id: str, range: str, values_json: str) -> str:
    """Writes values into a specific range of a Google Sheet, OVERWRITING whatever is there.
    Read the sheet first with sheets_read so you target the right cells. To only add new rows
    without overwriting, use sheets_append instead.

    Args:
        spreadsheet_id: The spreadsheet's id.
        range: The A1-notation range to write to, e.g. 'Sheet1!A2:C4'. Its size should match the values.
        values_json: A JSON 2D array of the cell values, e.g. '[["Ori", 22], ["Sam", 30]]'.
    """
    try:
        values = _parse_values(values_json)
        service = _sheets()
        result = service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=range,
            valueInputOption="USER_ENTERED",  # interprets formulas/dates like typing in the UI
            body={"values": values},
        ).execute()
        return f"Updated {result.get('updatedCells', 0)} cell(s) in {result.get('updatedRange', range)}."
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


def sheets_append(spreadsheet_id: str, values_json: str, range: str = "Sheet1") -> str:
    """Appends new rows to the bottom of a Google Sheet's existing data - a safe way to add
    entries without overwriting anything.

    Args:
        spreadsheet_id: The spreadsheet's id.
        values_json: A JSON 2D array of the rows to add, e.g. '[["Ori", 22], ["Sam", 30]]'.
        range: The tab (or A1 range) identifying the table to append to. Defaults to 'Sheet1'.
    """
    try:
        values = _parse_values(values_json)
        service = _sheets()
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range,
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        updated = result.get("updates", {})
        return f"Appended {updated.get('updatedRows', 0)} row(s) to {updated.get('updatedRange', range)}."
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"
